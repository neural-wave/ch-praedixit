"""Derive the schema actually used by the patient graphs (a VoID-like class/predicate profile).

The hackathon names SPHN schema 2026.1, but the mock patients follow 2024.2 in places
(e.g. sphn:hasAdmissionDateTime on AdministrativeCase, sphn:hasRank with named individuals).
A generated query must match the data, so the generator is grounded on what the graphs
contain, enriched with labels and definitions from the 2026.1 schema file.

For every class instantiated in the patient graphs this records:
  * instance and patient counts,
  * every predicate used on its instances with frequency,
  * for each predicate the object kinds: SPHN classes, terminology namespaces of code nodes
    (with the most frequent code classes), named individuals, or literal datatypes.

Output: runtime/resources/schema.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # repository root
RUNTIME = ROOT / "runtime"
WORK = ROOT / "preparation" / "work"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(ROOT))

from nl2sparql.prefixes import GLOBAL_GRAPH, PATIENT_GRAPH_PREFIX, compact_iri  # noqa: E402
from nl2sparql.sparql_client import SparqlClient  # noqa: E402

SPHN = "https://biomedit.ch/rdf/sphn-schema/sphn#"
PG_FILTER = f'FILTER(STRSTARTS(STR(?g), "{PATIENT_GRAPH_PREFIX}"))'


def run(client: SparqlClient, q: str) -> list[dict]:
    t = time.time()
    res = client.query(q, timeout=3600)
    if not res.ok:
        raise RuntimeError(f"{res.error}\n{q}")
    print(f"  {len(res.rows)} rows in {time.time() - t:.0f}s")
    return [{k: v["value"] for k, v in b.items()} for b in res.rows]


def namespace_of(iri: str) -> str:
    m = re.match(r"^https://biomedit\.ch/rdf/sphn-resource/(icd-10-gm|chop)/\d{4}/", iri)
    if m:
        return m.group(1) + ":"
    best = compact_iri(iri)
    if ":" in best and not best.startswith("<"):
        return best.split(":", 1)[0] + ":"
    # versioned ICD/CHOP or unknown: cut the last path segment
    cut = max(iri.rfind("/"), iri.rfind("#"))
    return iri[: cut + 1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="http://localhost:8890/sparql")
    ap.add_argument("--out", default=str(RUNTIME / "resources" / "schema.json"))
    args = ap.parse_args()
    client = SparqlClient(args.endpoint, timeout=3600, max_bytes=2_000_000_000)

    print("classes")
    classes = run(client, f"""
      SELECT ?c (COUNT(DISTINCT ?s) AS ?n) (COUNT(DISTINCT ?g) AS ?patients) WHERE {{
        GRAPH ?g {{ ?s a ?c }} {PG_FILTER} FILTER(STRSTARTS(STR(?c), "{SPHN}")) }} GROUP BY ?c""")
    n_patients = int(run(client, f"""SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ ?s a <{SPHN}SubjectPseudoIdentifier> }} {PG_FILTER} }}""")[0]["n"])

    print("class/predicate")
    cp = run(client, f"""
      SELECT ?c ?p (COUNT(*) AS ?n) WHERE {{
        GRAPH ?g {{ ?s a ?c ; ?p ?o }} {PG_FILTER} FILTER(STRSTARTS(STR(?c), "{SPHN}")) FILTER(?p != <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>)
      }} GROUP BY ?c ?p""")

    print("object classes (sphn)")
    obj_sphn = run(client, f"""
      SELECT ?c ?p ?oc (COUNT(*) AS ?n) WHERE {{
        GRAPH ?g {{ ?s a ?c ; ?p ?o . ?o a ?oc }} {PG_FILTER}
        FILTER(STRSTARTS(STR(?c), "{SPHN}")) FILTER(STRSTARTS(STR(?oc), "{SPHN}"))
      }} GROUP BY ?c ?p ?oc""")

    print("object code classes (terminologies)")
    obj_codes = run(client, f"""
      SELECT ?c ?p ?oc (COUNT(*) AS ?n) WHERE {{
        GRAPH ?g {{ ?s a ?c ; ?p ?o . ?o a ?oc }} {PG_FILTER}
        FILTER(STRSTARTS(STR(?c), "{SPHN}")) FILTER(!STRSTARTS(STR(?oc), "{SPHN}"))
      }} GROUP BY ?c ?p ?oc""")

    print("untyped IRI objects (named individuals)")
    obj_ind = run(client, f"""
      SELECT ?c ?p ?o (COUNT(*) AS ?n) WHERE {{
        GRAPH ?g {{ ?s a ?c ; ?p ?o . FILTER(isIRI(?o)) FILTER NOT EXISTS {{ ?o a ?any }} }} {PG_FILTER}
        FILTER(STRSTARTS(STR(?c), "{SPHN}"))
      }} GROUP BY ?c ?p ?o""")

    print("literal datatypes")
    obj_lit = run(client, f"""
      SELECT ?c ?p ?dt (COUNT(*) AS ?n) (SAMPLE(STR(?o)) AS ?example) WHERE {{
        GRAPH ?g {{ ?s a ?c ; ?p ?o . FILTER(isLiteral(?o)) BIND(DATATYPE(?o) AS ?dt) }} {PG_FILTER}
        FILTER(STRSTARTS(STR(?c), "{SPHN}"))
      }} GROUP BY ?c ?p ?dt""")

    print("schema labels and definitions (2026.1)")
    labels = run(client, f"""
      SELECT ?s ?label ?def WHERE {{ GRAPH <{GLOBAL_GRAPH}> {{
        ?s <http://www.w3.org/2000/01/rdf-schema#label> ?label .
        OPTIONAL {{ ?s <http://www.w3.org/2004/02/skos/core#definition> ?def }}
        FILTER(STRSTARTS(STR(?s), "{SPHN}")) }} }}""")
    parents = run(client, f"""
      SELECT ?s ?parent WHERE {{ GRAPH <{GLOBAL_GRAPH}> {{
        ?s <http://www.w3.org/2000/01/rdf-schema#subClassOf> ?parent .
        FILTER(STRSTARTS(STR(?s), "{SPHN}")) FILTER(STRSTARTS(STR(?parent), "{SPHN}")) }} }}""")
    ind_labels = run(client, f"""
      SELECT ?s ?label WHERE {{ GRAPH <{GLOBAL_GRAPH}> {{
        ?s <http://www.w3.org/2000/01/rdf-schema#label> ?label .
        FILTER(STRSTARTS(STR(?s), "https://biomedit.ch/rdf/sphn-schema/sphn/individual#")) }} }}""")

    meta = {r["s"]: {"label": r["label"], "definition": r.get("def", "")} for r in labels}
    ind_meta = {r["s"]: r["label"] for r in ind_labels}
    parent_map: dict[str, list[str]] = defaultdict(list)
    for r in parents:
        parent_map[compact_iri(r["s"])].append(compact_iri(r["parent"]))

    schema: dict = {"n_patients": n_patients, "classes": {}, "predicates": {}}
    for r in classes:
        c = compact_iri(r["c"])
        schema["classes"][c] = {
            "iri": r["c"],
            "label": meta.get(r["c"], {}).get("label", c.split(":")[-1]),
            "definition": meta.get(r["c"], {}).get("definition", ""),
            "instances": int(r["n"]),
            "patients": int(r["patients"]),
            "parents": parent_map.get(c, []),
            "predicates": {},
            "in_schema_2026_1": r["c"] in meta,
        }
    for r in cp:
        c, p = compact_iri(r["c"]), compact_iri(r["p"])
        schema["classes"].setdefault(c, {"predicates": {}, "label": c, "instances": 0, "patients": 0})
        schema["classes"][c]["predicates"][p] = {
            "count": int(r["n"]), "classes": {}, "code_systems": {}, "top_codes": {}, "individuals": {}, "datatypes": {},
        }
        pm = schema["predicates"].setdefault(p, {"label": meta.get(r["p"], {}).get("label", ""),
                                                 "definition": meta.get(r["p"], {}).get("definition", ""),
                                                 "in_schema_2026_1": r["p"] in meta, "domains": {}})
        pm["domains"][c] = int(r["n"])

    def slot(r: dict) -> dict:
        preds = schema["classes"][compact_iri(r["c"])]["predicates"]
        return preds.get(compact_iri(r["p"]), {"classes": {}, "code_systems": {}, "individuals": {}, "datatypes": {}})

    for r in obj_sphn:
        slot(r)["classes"][compact_iri(r["oc"])] = int(r["n"])
    code_tops: dict[tuple, list] = defaultdict(list)
    for r in obj_codes:
        s = slot(r)
        ns = namespace_of(r["oc"])
        s["code_systems"][ns] = s["code_systems"].get(ns, 0) + int(r["n"])
        code_tops[(compact_iri(r["c"]), compact_iri(r["p"]))].append((int(r["n"]), compact_iri(r["oc"])))
    for (c, p), lst in code_tops.items():
        lst.sort(reverse=True)
        schema["classes"][c]["predicates"][p]["top_codes"] = {code: n for n, code in lst[:20]}
        schema["classes"][c]["predicates"][p]["n_distinct_codes"] = len(lst)
    for r in obj_ind:
        s = slot(r)
        s["individuals"][compact_iri(r["o"])] = int(r["n"])
    for r in obj_lit:
        s = slot(r)
        dt = compact_iri(r.get("dt", "")) if r.get("dt") else "plain literal"
        s["datatypes"][dt] = {"count": int(r["n"]), "example": r.get("example", "")[:60]}
    schema["individual_labels"] = {compact_iri(k): v for k, v in ind_meta.items()}
    schema["sphn_schema_terms"] = {compact_iri(k): v["label"] for k, v in meta.items()}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(schema, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out}: {len(schema['classes'])} classes, {len(schema['predicates'])} predicates, {n_patients} patients")


if __name__ == "__main__":
    main()
