"""Data-derived schema: class shapes for the prompt and schema checks for generated queries."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .prefixes import PREFIXES
from .textindex import BM25, tokenize

BOILERPLATE = {
    "sphn:hasSubjectPseudoIdentifier",
    "sphn:hasSourceSystem",
    "sphn:hasDataProvider",
    "sphn:hasSharedIdentifier",
}
VERSIONED_RE = re.compile(r"^<https://biomedit\.ch/rdf/sphn-resource/(icd-10-gm|chop)/\d{4}/([^>]+)>$")
CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def split_camel(name: str) -> str:
    return CAMEL_RE.sub(" ", name.split(":")[-1].replace("has", "has ", 1) if name.startswith("sphn:has") else name.split(":")[-1])


def unversion(code: str) -> str:
    m = VERSIONED_RE.match(code)
    return f"{m.group(1)}:{m.group(2)}" if m else code


class Schema:
    def __init__(self, path: str | Path) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.n_patients: int = data["n_patients"]
        self.classes: dict = data["classes"]
        self.predicates: dict = data["predicates"]
        self.individual_labels: dict = data.get("individual_labels", {})
        self.schema_terms: dict = data.get("sphn_schema_terms", {})
        # Named individuals: those labelled in the 2026.1 schema plus those used by the data
        # (2024.2 individuals such as sphn-individual:Principal have no label in the global graph).
        self.data_individuals: dict[str, str] = {}
        for info in self.classes.values():
            for pinfo in info["predicates"].values():
                for ind in pinfo.get("individuals", {}):
                    if ind.startswith("sphn-individual:"):
                        self.data_individuals[ind] = self.individual_labels.get(ind) or split_camel(ind)
        self.known_individuals = set(self.individual_labels) | set(self.data_individuals)
        # Predicates whose objects are code nodes typed with terminology classes.
        self.code_predicates: dict[str, dict[str, int]] = {}
        for info in self.classes.values():
            for p, pinfo in info["predicates"].items():
                for ns, n in pinfo.get("code_systems", {}).items():
                    if not ns.startswith("sphn"):
                        slot = self.code_predicates.setdefault(p, {})
                        slot[ns] = slot.get(ns, 0) + n
        # Predicates pointing to sphn:Code nodes (identifier/name/coding-system strings, no terminology class)
        self.sphn_code_predicates: set[str] = {
            p for info in self.classes.values() for p, pinfo in info["predicates"].items()
            if "sphn:Code" in pinfo.get("classes", {}) and p not in self.code_predicates
        }
        self.incoming: dict[str, list[tuple[str, str, int]]] = {}
        for c, info in self.classes.items():
            for p, pinfo in info["predicates"].items():
                for oc, n in pinfo.get("classes", {}).items():
                    self.incoming.setdefault(oc, []).append((c, p, n))
        self._names = sorted(self.classes)
        docs = []
        for c in self._names:
            info = self.classes[c]
            text = " ".join(
                [split_camel(c), info.get("label", ""), info.get("definition", "")[:300]]
                + [split_camel(p) for p in info["predicates"] if p not in BOILERPLATE]
            )
            # class name and label weigh more than the definition
            docs.append(tokenize(f"{split_camel(c)} {split_camel(c)} {info.get('label', '')} {text}"))
        self._bm25 = BM25(docs)

    # ------------------------------------------------------------------ lookup
    def known_term(self, term: str) -> bool:
        return term in self.classes or term in self.predicates or term in self.schema_terms

    def class_has_predicate(self, cls: str, pred: str) -> bool:
        info = self.classes.get(cls)
        return bool(info) and pred in info["predicates"]

    def predicates_of(self, cls: str) -> list[str]:
        info = self.classes.get(cls, {})
        return sorted(info.get("predicates", {}), key=lambda p: -info["predicates"][p]["count"])

    def domains_of(self, pred: str) -> list[str]:
        return sorted(self.predicates.get(pred, {}).get("domains", {}),
                      key=lambda c: -self.predicates[pred]["domains"][c])

    def ranked_classes(self, question: str, k: int = 8) -> list[tuple[str, float]]:
        return [(self._names[i], s) for i, s in self._bm25.top(tokenize(question), k=k)]

    # --------------------------------------------------------------- rendering
    def render_class(self, cls: str, max_codes: int = 16, label_fn=None) -> str:
        info = self.classes.get(cls)
        if not info:
            return ""
        head = f"{cls}"
        label = info.get("label")
        if label and label.replace(" ", "") != cls.split(":")[-1]:
            head += f' "{label}"'
        head += f" ({info.get('instances', 0)} records, {info.get('patients', 0)} patients)"
        if info.get("parents"):
            head += " subClassOf " + ", ".join(info["parents"][:2])
        lines = [head]
        boiler = []
        preds = sorted(info["predicates"].items(), key=lambda kv: -kv[1]["count"])
        for p, pinfo in preds:
            if p in BOILERPLATE:
                boiler.append(p)
                continue
            lines.append(f"  {p} -> {self._render_range(pinfo, max_codes, label_fn)}")
        if boiler:
            lines.append("  also: " + ", ".join(sorted(boiler)))
        inc = sorted(self.incoming.get(cls, []), key=lambda x: -x[2])
        inc = [f"{c} {p}" for c, p, _ in inc if p not in BOILERPLATE][:4]
        if inc:
            lines.append("  used by: " + "; ".join(inc))
        return "\n".join(lines)

    def _render_range(self, pinfo: dict, max_codes: int, label_fn=None) -> str:
        parts = []
        if pinfo.get("classes"):
            parts.append(" | ".join(sorted(pinfo["classes"], key=lambda c: -pinfo["classes"][c])[:4]))
        if pinfo.get("code_systems"):
            systems = sorted(pinfo["code_systems"], key=lambda s: -pinfo["code_systems"][s])
            systems = [s for s in systems if s not in ("sphn:",)]
            tops = []
            for code in list(pinfo.get("top_codes", {})):
                u = unversion(code)
                if u not in tops and not u.startswith("sphn:"):
                    tops.append(u)
            n_codes = pinfo.get("n_distinct_codes", len(tops))
            sys_txt = "/".join(s.rstrip(":") if not s.startswith("http") else s for s in systems[:3])
            if n_codes <= len(tops) and n_codes <= max_codes:
                # a small value set: list every value, with its label, so the model can pick the right one
                shown = ", ".join(f"{c} ({label_fn(c)})" if label_fn and label_fn(c) else c for c in tops)
                parts.append(f"code node typed with one of: {shown}")
            else:
                # large value set: do not show sample codes (the model would copy them); codes come from the question
                parts.append(f"code node typed with a {sys_txt} class ({n_codes} distinct codes in the data)")
        if pinfo.get("individuals"):
            inds = sorted(pinfo["individuals"], key=lambda i: -pinfo["individuals"][i])
            parts.append(" | ".join(inds[:8]) + (" | ..." if len(inds) > 8 else ""))
        if pinfo.get("datatypes"):
            dts = sorted(pinfo["datatypes"], key=lambda d: -pinfo["datatypes"][d]["count"])
            parts.append(" | ".join(d for d in dts[:2]))
        return "; ".join(parts) or "?"

    def render(self, classes: list[str], label_fn=None) -> str:
        return "\n".join(self.render_class(c, label_fn=label_fn) for c in classes if c in self.classes)

    def class_list(self) -> str:
        """One line naming every class present in the data (cheap global overview)."""
        return ", ".join(c.split(":")[-1] for c in sorted(self.classes, key=lambda c: -self.classes[c]["patients"]))


def classes_in_query(query: str) -> list[str]:
    seen = []
    for m in re.finditer(r"\bsphn:[A-Z][A-Za-z0-9]*", query):
        if m.group(0) not in seen:
            seen.append(m.group(0))
    return seen


__all__ = ["Schema", "classes_in_query", "PREFIXES"]
