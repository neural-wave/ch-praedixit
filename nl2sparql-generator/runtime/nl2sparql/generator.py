"""The NL-to-SPARQL pipeline.

    question
      -> (1) analysis (LLM, short JSON): relevant SPHN classes and clinical concepts
             with English/German wording and a code guess per concept
      -> (2) grounding (no LLM): class shapes from the data-derived schema, verified
             candidate codes from the terminology index, similar validated examples (BM25)
      -> (3) generation (LLM): compact SPARQL (no PREFIX lines, `isa` for inferred typing)
      -> (4) deterministic post-processing: expansion, prefixes, inference on code classes
      -> (5) validation: schema/terminology/semantic checks, compilation by Virtuoso
      -> (6) repair (LLM, at most N rounds) when a blocking problem is found

Only this package, resources/ and config.json are needed at inference time.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .examples import ExampleBank
from .llm import LLMUsage, make_llm
from .prompts import CODES_TEMPLATE, CONTEXT_TEMPLATE, EXAMPLE_TEMPLATE, REPAIR_TEMPLATE, SYSTEM_PROMPT
from .schema import Schema, classes_in_query, split_camel
from .shorthand import expand
from .sparql_client import SparqlClient
from .terminology import SYSTEM_OF_PREFIX, Candidate, Terminology, format_candidates, label_matches_question
from .validate import (
    Problem,
    check_code_meaning,
    check_execution,
    check_schema,
    check_semantics,
    empty_result_problem,
)

ROOT = Path(__file__).resolve().parent.parent

ANALYSIS_PROMPT = """You prepare the translation of a question about a clinical knowledge graph into SPARQL.
Record classes available in the graph (sphn: prefix omitted):
{catalog}

For the question, return JSON with:
- "classes": the 1-6 record classes above needed to answer it (exact names),
- "concepts": the clinical concepts that must be matched to a terminology code (diseases, procedures, drugs,
  lab tests, units, organisms, genotypes, ...), each as {{"text": words in the question, "english": English
  medical term, "german": German medical term, "system": one of ICD-10-GM, CHOP, SNOMED CT, LOINC, UCUM, GENO,
  SO, ORDO, ATC, "code": the code if you know it or ""}}. Diagnoses on billing/oncology records use ICD-10-GM,
  billed procedures use CHOP, lab tests LOINC, units UCUM, most other coded values SNOMED CT.
  Do not list concepts that are just record types, sex, dates or numbers.
Return only the JSON object."""

DEFAULT_CONFIG = {
    "backend": "ollama",
    "model": "qwen2.5-coder:7b",
    "llm_url": "http://localhost:11434",
    "endpoint": "http://localhost:8890/sparql",
    "use_endpoint": True,
    "num_ctx": 8192,
    "analysis": True,
    "n_examples": 6,
    "max_repairs": 2,
    "repair_temperature": 0.3,
    "repair_on_warnings": ["no_graph", "fanout", "cross_patient", "code_label", "empty"],
    "check_code_meaning": True,
    "example_bank": "resources/examples.jsonl",
    "schema": "resources/schema.json",
    "terminology": "resources/terminology.sqlite",
    "max_schema_classes": 7,
    "max_codes": 10,
    "execution_timeout": 20,
}


def _example_query(compact_query: str) -> str:
    """Example as shown to the model: no Virtuoso join-order pragma (template-generated examples
    carry DEFINE sql:select-option "order", which the official examples do not use)."""
    return re.sub(r"^\s*DEFINE\s+sql:select-option[^\n]*\n", "", compact_query, flags=re.IGNORECASE).strip()


@dataclass
class GenerationResult:
    """One translation. `usage` counts only the LLM calls of this translation.

    blocking_problems: the problems of `problems` worth a repair (errors and the warnings listed in
    repair_on_warnings), the ones Generator.revise reports to the model.
    question, prompt_messages and raw are what Generator.revise needs to continue the conversation:
    the question, the messages sent before the first attempt, and the chosen query as the model
    wrote it (compact form).
    """
    sparql: str
    valid: bool
    problems: list[dict] = field(default_factory=list)
    blocking_problems: list[dict] = field(default_factory=list)
    attempts: int = 0
    seconds: float = 0.0
    usage: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)
    question: str = ""
    prompt_messages: list[dict] = field(default_factory=list)
    raw: str = ""


def load_config(path: str | Path | None = None, **overrides) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path:
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    return cfg


class Generator:
    def __init__(self, config: dict | None = None, bank_filter=None) -> None:
        self.cfg = dict(DEFAULT_CONFIG)
        if config:
            self.cfg.update(config)
        res = lambda p: (ROOT / p) if not Path(p).is_absolute() else Path(p)  # noqa: E731
        self.schema = Schema(res(self.cfg["schema"]))
        self.terminology = Terminology(res(self.cfg["terminology"]))
        self.bank = ExampleBank(res(self.cfg["example_bank"]), exclude_fn=bank_filter)
        self.llm = make_llm(self.cfg)
        self.client = SparqlClient(self.cfg["endpoint"]) if self.cfg.get("use_endpoint") else None
        if self.client is not None and not self.client.is_up():
            self.client = None
        self.catalog = self._catalog()

    # ----------------------------------------------------------------- stage 1
    def _catalog(self) -> str:
        items = []
        for c in sorted(self.schema.classes, key=lambda c: c.lower()):
            name = c.split(":", 1)[1]
            label = self.schema.classes[c].get("label") or ""
            if label and label.replace(" ", "").lower() != name.lower():
                items.append(f"{name} ({label})")
            else:
                items.append(name)
        return ", ".join(items)

    def analyse(self, question: str, usage: LLMUsage) -> dict:
        messages = [
            {"role": "system", "content": ANALYSIS_PROMPT.format(catalog=self.catalog)},
            {"role": "user", "content": question},
        ]
        raw = self.llm.chat(messages, usage, max_tokens=400)
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        try:
            data = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            data = {}
        classes = []
        for c in data.get("classes", []) or []:
            if not isinstance(c, str):
                continue
            c = c.strip()
            c = c if c.startswith("sphn:") else f"sphn:{c}"
            if c in self.schema.classes and c not in classes:
                classes.append(c)
        concepts = [c for c in (data.get("concepts") or []) if isinstance(c, dict)]
        return {"classes": classes, "concepts": concepts, "raw": raw}

    # ----------------------------------------------------------------- stage 2
    def select_classes(self, question: str, analysis: dict | None, examples: list[dict]) -> list[str]:
        chosen: list[str] = []
        if analysis:
            chosen += analysis["classes"]
        for c, _ in self.schema.ranked_classes(question, k=3):
            if c not in chosen:
                chosen.append(c)
        for ex in examples[:2]:
            for c in ex.get("classes", []):
                if c in self.schema.classes and c not in chosen:
                    chosen.append(c)
        # one hop: classes the chosen ones point to (e.g. Birth -> GestationalAgeAtBirth)
        hops = []
        for c in chosen[:4]:
            for p, pinfo in self.schema.classes[c]["predicates"].items():
                if p in ("sphn:hasSubjectPseudoIdentifier", "sphn:hasSourceSystem", "sphn:hasDataProvider"):
                    continue
                for oc in pinfo.get("classes", {}):
                    if oc not in chosen and oc not in hops and oc not in ("sphn:Code", "sphn:Unit"):
                        q_tokens = set(question.lower().split())
                        if any(w in q_tokens for w in split_camel(oc).lower().split()) or oc in ("sphn:Quantity",):
                            hops.append(oc)
        return (chosen + hops)[: self.cfg["max_schema_classes"]]

    def systems_for(self, classes: list[str]) -> list[str]:
        """Terminologies actually used by the coded predicates of these classes in the data."""
        systems = []
        for c in classes:
            for p, pinfo in self.schema.classes.get(c, {}).get("predicates", {}).items():
                if p in ("sphn:hasSubjectPseudoIdentifier", "sphn:hasSourceSystem", "sphn:hasDataProvider"):
                    continue
                for ns in pinfo.get("code_systems", {}):
                    name = SYSTEM_OF_PREFIX.get(ns.rstrip(":"))
                    if name and name not in systems:
                        systems.append(name)
                if pinfo.get("individuals") and "SPHN individual" not in systems:
                    systems.append("SPHN individual")
        return systems

    def ground_codes(self, question: str, analysis: dict | None, classes: list[str]) -> list[Candidate]:
        systems = self.systems_for(classes)
        if not systems:
            # no coded predicate on the relevant classes: only codes written in the question itself
            return self.terminology._explicit_codes(question)
        found: dict[str, Candidate] = {}

        def add(c: Candidate) -> None:
            prev = found.get(c.curie)
            if prev is None or c.coverage > prev.coverage:
                found[c.curie] = c

        for c in self.terminology._explicit_codes(question):
            add(c)
        if "SPHN individual" in systems:
            for c in self._individual_candidates(question, classes):
                add(c)
        if analysis:
            for concept in analysis["concepts"][:6]:
                system = str(concept.get("system") or "")
                code = str(concept.get("code") or "").strip()
                prefix = {v: k for k, v in SYSTEM_OF_PREFIX.items()}.get(system)
                if code and prefix and system in systems:
                    code = code.split(":")[-1]
                    row = self.terminology.get(f"{prefix}:{code}")
                    wording = " ".join(str(concept.get(k) or "") for k in ("text", "english", "german"))
                    # keep a guessed code only when one of its labels fits the concept's wording
                    if row and label_matches_question(self.terminology.labels_of(row["curie"]), wording):
                        add(Candidate(row["curie"], row["system"], row["label"], concept.get("text", ""), 0.9,
                                      row["data_records"]))
                sys_list = [system] if system in systems else systems
                for term in (concept.get("english"), concept.get("german"), concept.get("text")):
                    if not term or not isinstance(term, str):
                        continue
                    for c in self.terminology.search(term, sys_list, k=3):
                        if c.coverage >= 0.6:
                            c.matched = concept.get("text") or term
                            add(c)
        for c in self.terminology.candidates_for_question(question, systems, k=self.cfg["max_codes"],
                                                          min_coverage=0.6):
            add(c)
        ranked = sorted(found.values(), key=lambda c: (-c.coverage, -(c.data_records > 0), len(c.label)))
        return ranked[: self.cfg["max_codes"]]

    def _code_label(self, curie: str) -> str | None:
        row = self.terminology.get(curie)
        if not row:
            return None
        label = re.sub(r"\s*\([^)]*\)$", "", row["label"])
        return label[:60]

    def _individual_candidates(self, question: str, classes: list[str]) -> list[Candidate]:
        """Named individuals used by the selected classes whose name appears in the question."""
        out = []
        for c in classes:
            for p, pinfo in self.schema.classes.get(c, {}).get("predicates", {}).items():
                for ind, n in pinfo.get("individuals", {}).items():
                    label = self.schema.data_individuals.get(ind)
                    if label and label_matches_question([label], question):
                        out.append(Candidate(ind, "SPHN individual", f"{label} (value of {c} {p})", label, 0.95, n))
        return out

    # ----------------------------------------------------------------- stage 3
    def build_messages(self, question: str, classes: list[str], codes: list[Candidate], examples: list[dict]) -> list[dict]:
        ordered = list(reversed(examples))  # most similar example closest to the question
        ex_txt = "\n\n".join(EXAMPLE_TEMPLATE.format(question=e["question"], query=_example_query(e["compact"]))
                               for e in ordered)
        codes_block = CODES_TEMPLATE.format(codes=format_candidates(codes)) if codes else ""
        context = CONTEXT_TEMPLATE.format(
            n_patients=self.schema.n_patients,
            schema=self.schema.render(classes, label_fn=self._code_label),
            codes_block=codes_block,
            examples=ex_txt or "(none)",
            question=question,
        )
        return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": context}]

    # ----------------------------------------------------------------- stage 5
    def validate(self, query: str, question: str) -> tuple[list[Problem], object]:
        problems = check_semantics(query, self.schema, question) + check_schema(query, self.schema, self.terminology)
        if self.cfg.get("check_code_meaning", True):
            problems += check_code_meaning(query, question, self.terminology)
        res = None
        if self.client is not None and not any(p.code == "unbalanced" for p in problems):
            exec_problems, res = check_execution(query, self.client, timeout=self.cfg["execution_timeout"])
            problems += exec_problems
            if not exec_problems:
                p = empty_result_problem(res, query, self.terminology)
                if p:
                    problems.append(p)
        return problems, res

    def _blocking(self, problems: list[Problem]) -> list[Problem]:
        return [p for p in problems if p.severity == "error" or p.code in self.cfg["repair_on_warnings"]]

    # ------------------------------------------------------------------- main
    def generate(self, question: str) -> GenerationResult:
        start = time.perf_counter()
        usage = LLMUsage()
        trace: dict = {}
        analysis = self.analyse(question, usage) if self.cfg["analysis"] else None
        trace["analysis"] = analysis
        search_text = question
        if analysis:
            extra = " ".join(str(c.get("english", "")) for c in analysis["concepts"])
            extra += " " + " ".join(split_camel(c) for c in analysis["classes"])
            search_text = f"{question} {extra}"
        examples = self.bank.search(search_text, k=self.cfg["n_examples"])
        classes = self.select_classes(question, analysis, examples)
        codes = self.ground_codes(question, analysis, classes)
        trace.update({"classes": classes, "codes": [c.curie for c in codes], "examples": [e["id"] for e in examples]})
        messages = self.build_messages(question, classes, codes, examples)
        return self._write_query(question, messages, usage, trace, start)

    def revise(self, previous: GenerationResult) -> GenerationResult:
        """Write the query of `previous` again, after telling the model its blocking problems.

        The conversation of `previous` continues with its query and REPAIR_TEMPLATE listing
        previous.blocking_problems: no new analysis or grounding. Every attempt samples at
        repair_temperature, as the repairs of generate do. The returned usage counts only the calls
        of this revision.
        """
        start = time.perf_counter()
        usage = LLMUsage()
        trace: dict = {"revision_of": previous.sparql, "reported_problems": previous.blocking_problems}
        messages = previous.prompt_messages + [
            {"role": "assistant", "content": previous.raw},
            {"role": "user", "content": REPAIR_TEMPLATE.format(
                problems="\n".join(f"- {p['message']}" for p in previous.blocking_problems))},
        ]
        return self._write_query(previous.question, messages, usage, trace, start, is_revision=True)

    def _write_query(self, question: str, messages: list[dict], usage: LLMUsage, trace: dict,
                     start: float, is_revision: bool = False) -> GenerationResult:
        """Ask the model for the query, validate it and repair it up to max_repairs times; keep the best attempt.

        The first attempt of a new translation is greedy; repairs, and every attempt of a revision
        (`is_revision`), sample at repair_temperature.
        """
        prompt_messages = messages
        best: tuple[int, str, str, list[Problem]] | None = None
        attempts = 0
        for attempt in range(self.cfg["max_repairs"] + 1):
            attempts += 1
            # repairs sample a little so the model can leave a fixed point (rewriting the same query)
            if attempt == 0 and not is_revision:
                temperature, seed = None, None
            else:
                temperature, seed = self.cfg.get("repair_temperature", 0.3), 1000 + attempt
            raw = self.llm.chat(messages, usage, max_tokens=1500, temperature=temperature, seed=seed)
            try:
                query = expand(raw)
            except Exception as exc:  # pragma: no cover - defensive
                query, problems = raw, [Problem("error", "postprocess", str(exc))]
            else:
                problems, _ = self.validate(query, question)
            blocking = self._blocking(problems)
            score = sum(1 for p in problems if p.severity == "error") * 10 + len(blocking)
            if best is None or score < best[0]:
                best = (score, query, raw, problems)
            trace.setdefault("attempts", []).append(
                {"query": query, "problems": [p.__dict__ for p in problems]})
            if not blocking:
                break
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": REPAIR_TEMPLATE.format(
                    problems="\n".join(f"- {p.message}" for p in blocking))},
            ]
        assert best is not None
        _, query, raw, problems = best
        return GenerationResult(
            sparql=query,
            valid=not any(p.severity == "error" for p in problems),
            problems=[p.__dict__ for p in problems],
            blocking_problems=[p.__dict__ for p in self._blocking(problems)],
            attempts=attempts,
            seconds=round(time.perf_counter() - start, 3),
            usage=usage.as_dict(),
            trace=trace,
            question=question,
            prompt_messages=prompt_messages,
            raw=raw,
        )


__all__ = ["Generator", "GenerationResult", "load_config", "classes_in_query"]
