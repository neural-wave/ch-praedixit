"""Checks applied to every generated query before it is returned.

* syntax: Virtuoso itself compiles the query (the target engine; a short execution with a
  server-side timeout, so a slow but valid query is not reported as an error);
* schema: SPHN classes/predicates exist in the data, predicates fit the class of their
  subject, terminology constants exist;
* semantics (lint): patient patterns inside GRAPH ?g, patient counts use DISTINCT, numeric
  comparisons on quantities fix a unit, no cross-patient Cartesian products, no duplicate
  counting over independent joins.

Problems are returned as short natural-language messages that the repair step feeds back to
the model. Severity "error" triggers a repair; "warning" only does when configured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .schema import Schema
from .sparql_client import SparqlClient
from .sparql_parse import analyse, components
from .terminology import SYSTEM_OF_PREFIX, Terminology, label_matches_question, terminology_constants

SEX_CODES = {"snomed:248153007", "snomed:248152002", "snomed:32570681000036106"}

RDF_TYPE = "rdf:type"


@dataclass
class Problem:
    severity: str  # "error" | "warning"
    code: str
    message: str


def _subject_types(triples) -> dict[str, set[str]]:
    types: dict[str, set[str]] = {}
    for t in triples:
        if t.p == RDF_TYPE and t.o.startswith("sphn:"):
            types.setdefault(t.s, set()).add(t.o)
    return types


def check_schema(query: str, schema: Schema, terminology: Terminology | None) -> list[Problem]:
    problems: list[Problem] = []
    facts = analyse(query)
    types = _subject_types(facts.triples)
    # 1. unknown SPHN terms
    for term in sorted(set(re.findall(r"\bsphn:[A-Za-z][A-Za-z0-9]*", re.sub(r'"[^"]*"', "", query)))):
        if not schema.known_term(term):
            hint = ""
            close = [c for c in list(schema.classes) + list(schema.predicates)
                     if c.lower().startswith(term.lower()[:9])][:5]
            if close:
                hint = f" Similar terms: {', '.join(close)}."
            problems.append(Problem("error", "unknown_term", f"{term} does not exist in the SPHN schema or the data.{hint}"))
        elif term[5].isupper() and term not in schema.classes:
            problems.append(Problem("warning", "class_not_in_data",
                                    f"{term} is defined in SPHN 2026.1 but has no instances in the data."))
        elif term[5].islower() and term not in schema.predicates and term not in ("sphn:hasValue",):
            problems.append(Problem("error", "predicate_not_in_data",
                                    f"{term} is defined in SPHN 2026.1 but is never used in the data (the data follows an "
                                    f"older SPHN version); use a predicate listed in the schema."))
    # 2. predicate fits the subject's class
    for t in facts.triples:
        first = t.p.split("/")[0].lstrip("^(")
        if not first.startswith("sphn:") or t.s not in types:
            continue
        cls = types[t.s]
        if any(schema.class_has_predicate(c, first) for c in cls):
            continue
        if not any(c in schema.classes for c in cls):
            continue
        c0 = sorted(cls)[0]
        preds = ", ".join(p for p in schema.predicates_of(c0) if p not in (
            "sphn:hasSubjectPseudoIdentifier", "sphn:hasSourceSystem", "sphn:hasDataProvider"))[:600]
        doms = ", ".join(schema.domains_of(first)[:6])
        problems.append(Problem(
            "error", "predicate_domain",
            f"{t.s} is a {c0}, but {c0} records never use {first} in the data"
            + (f" ({first} is used on: {doms})" if doms else "")
            + f". {c0} uses: {preds}."))
    # 3. terminology constants exist
    if terminology is not None:
        for curie in terminology_constants(query):
            prefix = curie.split(":", 1)[0]
            if prefix not in SYSTEM_OF_PREFIX or prefix in ("sphn-individual",):
                if prefix == "sphn-individual" and curie not in schema.known_individuals:
                    problems.append(Problem("error", "unknown_individual", f"{curie} is not a known SPHN named individual."))
                continue
            if prefix in ("geno", "so", "obi", "efo", "edam"):
                continue  # small ontologies, not all codes indexed with labels
            row = terminology.get(curie)
            if row is None and not re.search(r"\d{4}/", curie):
                wildcard = re.search(re.escape(curie) + r"[*%]", query)
                hint = (" SPARQL has no code wildcards: to match a code range use isa on the parent code (e.g. a "
                        "chapter or block such as icd-10-gm:C00-C97), or `?code a ?concept . FILTER(STRSTARTS(STR(?concept), "
                        "STR(" + prefix + ":)))`.") if wildcard else ""
                problems.append(Problem("error", "unknown_code",
                                        f"{curie} is not a code of {SYSTEM_OF_PREFIX[prefix]}.{hint}"))
    return problems


def check_semantics(query: str, schema: Schema, question: str = "") -> list[Problem]:
    problems: list[Problem] = []
    facts = analyse(query)
    if facts.brace_balance != 0:
        problems.append(Problem("error", "unbalanced", "Curly braces are unbalanced."))
    if facts.paren_balance != 0:
        problems.append(Problem("error", "unbalanced", "Parentheses are unbalanced."))
    where_facts = facts
    if facts.form in ("CONSTRUCT", "INSERT", "DELETE"):
        m = re.search(r"\bWHERE\b", query, flags=re.IGNORECASE)
        where_facts = analyse(query[m.start():]) if m else facts  # templates are not graph patterns
    patient_triples = [t for t in where_facts.triples if t.o.startswith("sphn:") or t.p.startswith("sphn:")]
    outside = [t for t in patient_triples if t.graph is None]
    if outside:
        problems.append(Problem(
            "error", "no_graph",
            "Patient data patterns must be inside GRAPH ?g { ... } (each patient is a named graph): "
            + "; ".join(f"{t.s} {t.p} {t.o}" for t in outside[:3])))
    # COUNT(?g) without DISTINCT counts records, not patients
    if re.search(r"COUNT\s*\(\s*\?g\s*\)", query, flags=re.IGNORECASE) and "SELECT DISTINCT" not in query.upper():
        problems.append(Problem("warning", "count_g", "COUNT(?g) counts rows; use COUNT(DISTINCT ?g) for patients."))
    # Units on compared quantities
    value_vars = {t.o for t in facts.triples if t.p.endswith("sphn:hasValue") and t.o.startswith("?")}
    unit_constrained = any("sphn:hasUnit" in t.p for t in facts.triples)
    if value_vars and not unit_constrained:
        used_in_math = any(re.search(rf"(\b(AVG|SUM|MIN|MAX)\s*\(\s*\{v}\b|\{v}\s*[<>]=?|[<>]=?\s*\{v}\b|FLOOR\s*\(\s*\{v})",
                                     query, flags=re.IGNORECASE) for v in value_vars)
        if used_in_math:
            problems.append(Problem(
                "warning", "unit",
                "Values are compared or aggregated without fixing their unit (sphn:hasUnit/sphn:hasCode ?u . ?u a ucum:...);"
                " the same measurement is stored in several units."))
    # Disconnected patterns inside one patient graph -> duplicate counting risk
    by_graph: dict[str, list] = {}
    for t in facts.triples:
        if t.graph and t.graph.startswith("?") and t.optional_depth == 0:
            by_graph.setdefault(t.graph, []).append(t)
    for g, triples in by_graph.items():
        comps = [c - {g} for c in components([type(t)(t.s, t.p, t.o, None, 0) for t in triples])]
        comps = [c for c in comps if c]
        if len(comps) > 1:
            non_distinct = re.findall(r"\b(COUNT|SUM|AVG)\s*\(\s*(?!DISTINCT)(\*|\?\w+)", query, flags=re.IGNORECASE)
            non_distinct = [(f, v) for f, v in non_distinct if v != g]
            if non_distinct:
                problems.append(Problem(
                    "warning", "fanout",
                    f"Inside GRAPH {g} the patterns form {len(comps)} unconnected groups "
                    f"({'; '.join(sorted(min(c) for c in comps))}); every combination of their matches is a row, so "
                    f"{', '.join(f'{f}({v})' for f, v in non_distinct[:3])} over-counts. Link them (e.g. through "
                    "sphn:hasAdministrativeCase) or aggregate per patient / use DISTINCT."))
    # Cross-patient Cartesian product: two different graph variables never joined
    graph_vars = {t.graph for t in facts.triples if t.graph and t.graph.startswith("?")}
    if len(graph_vars) > 1:
        comps = components(facts.triples)
        gv_groups = [c & graph_vars for c in comps if c & graph_vars]
        if len(gv_groups) > 1 and not re.search(r"\bUNION\b", query, flags=re.IGNORECASE):
            problems.append(Problem(
                "warning", "cross_patient",
                f"Graph variables {', '.join(sorted(graph_vars))} are not joined: this pairs every patient with every "
                "other patient. Use the same ?g when the facts belong to the same patient."))
    # `isa icd-10-gm:` with no code (the model meant "any ICD code")
    for t in facts.triples:
        if t.p == RDF_TYPE and re.fullmatch(r"[A-Za-z][\w-]*:", t.o):
            problems.append(Problem(
                "error", "bare_namespace",
                f"`{t.s} isa {t.o}` names no code. To match or report any code of that terminology write "
                f"`{t.s} a ?concept . FILTER(STRSTARTS(STR(?concept), STR({t.o})))`."))
    # One code-node variable shared by records of different classes: each record has its own node
    code_subjects: dict[str, set[str]] = {}
    subj_types = _subject_types(facts.triples)
    for t in facts.triples:
        if t.p.split("/")[-1] in schema.code_predicates and t.o.startswith("?") and t.s.startswith("?"):
            code_subjects.setdefault(t.o, set()).add(t.s)
    for v, subs in code_subjects.items():
        classes = {c for s_ in subs for c in subj_types.get(s_, set())}
        if len(subs) > 1 and len(classes) > 1:
            problems.append(Problem(
                "error", "shared_code_var",
                f"{v} is used as the code node of {', '.join(sorted(subs))} ({', '.join(sorted(classes))}); every record "
                "has its own code node, so this join matches nothing. Use a different variable per record."))
    # Code nodes projected or grouped: their IRIs are per-patient resources, not codes
    code_vars = {}
    for t in facts.triples:
        last = t.p.split("/")[-1]
        if last in schema.code_predicates and t.o.startswith("?"):
            code_vars[t.o] = last
        elif last in schema.sphn_code_predicates and t.o.startswith("?"):
            code_vars.setdefault(t.o, last)
    group_vars = set()
    gm = re.search(r"GROUP\s+BY\s+((?:\s*\?\w+)+)", query, flags=re.IGNORECASE)
    if gm:
        group_vars = set(re.findall(r"\?\w+", gm.group(1)))
    for v, pred in code_vars.items():
        if v in facts.projected or v in group_vars:
            if pred not in schema.code_predicates:
                problems.append(Problem(
                    "error", "code_node_output",
                    f"{v} (object of {pred}) is a sphn:Code node: its IRI is a patient-specific resource. Report "
                    f"`{v} sphn:hasIdentifier ?code_id` (or sphn:hasName), or a category predicate of the record instead."))
                continue
            counts = schema.code_predicates[pred]
            main = max(counts, key=counts.get).rstrip(":")
            problems.append(Problem(
                "error", "code_node_output",
                f"{v} (object of {pred}) is a code node: its IRI is a patient-specific resource, not a code. To report "
                f"or group by the code write `{v} a ?concept . FILTER(STRSTARTS(STR(?concept), STR({main}:)))` "
                f"and use ?concept."))
    # Labels live in the global terminology graph, never in a patient graph
    for t in facts.triples:
        if t.p in ("rdfs:label", "skos:prefLabel") and t.graph and t.graph.startswith("?"):
            problems.append(Problem(
                "error", "label_in_patient_graph",
                f"`{t.s} {t.p} {t.o}` is inside GRAPH {t.graph}, but labels are only in the terminology graph: write "
                f"`{t.s} a ?concept .` in the patient graph and `GRAPH <https://biomedit.ch/rdf/sphn-resource/global> "
                f"{{ ?concept rdfs:label {t.o} }}` (for a code node, take the label of its class ?concept)."))
    # A patient graph group with no triple pattern binds nothing
    if re.search(r"GRAPH\s+\?\w+\s*\{\s*(FILTER|OPTIONAL|BIND|\})", query, flags=re.IGNORECASE):
        problems.append(Problem(
            "error", "no_anchor",
            "A GRAPH ?g { ... } group starts without a triple pattern, so it does not enumerate patients. Anchor every "
            "patient with `GRAPH ?g { ?patient a sphn:SubjectPseudoIdentifier }` and put FILTER NOT EXISTS / OPTIONAL "
            "after it."))
    # Units are UCUM codes
    unit_vars = {t.o for t in facts.triples if t.p.endswith("sphn:hasUnit/sphn:hasCode") and t.o.startswith("?")}
    unit_vars |= {t2.o for t1 in facts.triples if t1.p == "sphn:hasUnit" and t1.o.startswith("?")
                  for t2 in facts.triples if t2.s == t1.o and t2.p == "sphn:hasCode" and t2.o.startswith("?")}
    for t in facts.triples:
        if t.s in unit_vars and t.p == RDF_TYPE and ":" in t.o and not t.o.startswith(("ucum:", "?")):
            problems.append(Problem(
                "error", "unit_not_ucum",
                f"Units are coded with UCUM: `{t.s} a {t.o}` never matches; use the UCUM class of the unit "
                "(e.g. ucum:Cel, ucum:kg, ucum:a, ucum:mmsblHgsbr; see CANDIDATE CODES)."))
    # VALUES of versioned-terminology codes matched with `?node a ?cls` (no inference possible)
    for m in re.finditer(r"VALUES\s*(\?\w+)\s*\{([^}]*)\}", query, flags=re.IGNORECASE):
        var, body = m.group(1), m.group(2)
        if re.search(r"\b(icd-10-gm|chop|sphn-atc|atc):", body):
            used_as_class = [t for t in facts.triples if t.p == RDF_TYPE and t.o == var]
            if used_as_class:
                codes = " ".join(re.findall(r"\b(?:icd-10-gm|chop):[\w.\-]+", body)[:3])
                problems.append(Problem(
                    "error", "values_codes",
                    f"VALUES {var} {{ {codes} ... }} with `{used_as_class[0].s} a {var}` matches nothing: ICD-10-GM and "
                    "CHOP codes are stored as versioned classes and only match with isa. Write "
                    f"{{ {used_as_class[0].s} isa CODE1 }} UNION {{ {used_as_class[0].s} isa CODE2 }}, or isa a parent code."))
    if facts.form == "UNKNOWN":
        problems.append(Problem("error", "form", "No SPARQL query form (SELECT, ASK, CONSTRUCT, ...) found."))
    # Virtuoso REPLACE() with a pattern that can match the empty string fails at runtime
    for m in re.finditer(r"REPLACE\s*\([^,]+,\s*\"([^\"]*)\"", query, flags=re.IGNORECASE):
        pat = m.group(1)
        try:
            if re.fullmatch(pat.replace("\\\\", "\\"), ""):
                problems.append(Problem("error", "replace_empty",
                                        f'REPLACE pattern "{pat}" can match an empty string, which Virtuoso rejects.'))
        except re.error:
            pass
    return problems


VIRTUOSO_HINTS = [
    ("syntax error at 'UNION'", "UNION joins two brace groups: { patterns A } UNION { patterns B }."),
    ("outside aggregate and not mentioned in GROUP BY",
     "every projected variable must be in GROUP BY or inside an aggregate; HAVING must repeat the aggregate "
     "expression, not its alias."),
    ("Undefined namespace prefix", "use only the prefixes listed in the instructions (sphn:, snomed:, icd-10-gm:, ...)."),
    ("syntax error at 'AS'", "a computed projection needs parentheses: (EXPRESSION AS ?name)."),
    ("syntax error at 'OPTION'", "OPTION (inference ...) must directly follow the object of a triple pattern."),
    ("Variable", "check that every variable used in FILTER/BIND/ORDER BY is bound in the same group."),
]


def check_execution(query: str, client: SparqlClient, timeout: int = 20) -> tuple[list[Problem], object]:
    res = client.query(query, timeout=timeout)
    if res.ok:
        return [], res
    err = res.error or "unknown error"
    if err.startswith("not executed"):
        return [], None  # update/federated queries are never run; static checks still apply
    if "timeout" in err.lower() or "S1TAT" in err or "anytime" in err.lower():
        return [], res
    hint = ""
    for needle, text in VIRTUOSO_HINTS:
        if needle.lower() in err.lower():
            hint = " Hint: " + text
            break
    return [Problem("error", "virtuoso", f"Virtuoso rejected the query: {err[:400]}{hint}")], res


def empty_result_problem(res, query: str, terminology: Terminology | None) -> Problem | None:
    """Flag queries that return nothing although every code they use occurs in the data."""
    if res is None or not getattr(res, "ok", False) or res.boolean is not None:
        return None
    rows = res.rows
    empty = not rows
    if rows and len(rows) == 1:
        vals = [v.get("value") for v in rows[0].values()]
        if vals and all(v in ("0", "", None) for v in vals):
            empty = True
    if not empty:
        return None
    if terminology is not None:
        for curie in terminology_constants(query):
            row = terminology.get(curie)
            if row is not None and not row["data_records"]:
                return None  # a code absent from the sample explains an empty answer
    return Problem("warning", "empty",
                   "The query returns no data on the store although its codes occur in the data: a predicate path, "
                   "class or filter is probably wrong.")


def check_code_meaning(query: str, question: str, terminology: Terminology | None) -> list[Problem]:
    """Warn when a fixed clinical code has no label related to the wording of the question."""
    if terminology is None or not question:
        return []
    problems = []
    for curie in terminology_constants(query):
        prefix = curie.split(":", 1)[0]
        if prefix not in ("icd-10-gm", "chop", "snomed", "loinc", "ordo") or curie in SEX_CODES:
            continue
        code = curie.split(":", 1)[1]
        if code.lower() in question.lower():
            continue  # the question names the code itself
        labels = terminology.labels_of(curie)
        if labels and not label_matches_question(labels, question):
            problems.append(Problem(
                "warning", "code_label",
                f"{curie} means '{labels[0]}', which does not appear to be what the question asks for; "
                "use the code of the concept named in the question (see CANDIDATE CODES)."))
    return problems
