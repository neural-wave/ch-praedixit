"""Unit tests for the deterministic parts (no model, no store needed).

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # repository root
RUNTIME = ROOT / "runtime"
WORK = ROOT / "preparation" / "work"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(ROOT))

from evaluation.compare import compare  # noqa: E402
from nl2sparql.prefixes import add_prefixes, used_prefixes  # noqa: E402
from nl2sparql.shorthand import compact, expand, extract_query  # noqa: E402
from nl2sparql.sparql_client import QueryResult, is_safe_to_execute  # noqa: E402
from nl2sparql.sparql_parse import analyse  # noqa: E402

INF = 'OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global")'


def body(q: str) -> str:
    return " ".join(line for line in q.splitlines() if not line.startswith("PREFIX")).strip()


class ShorthandTests(unittest.TestCase):
    def test_isa_expands_with_inference(self):
        q = expand("SELECT * WHERE { GRAPH ?g { ?c isa icd-10-gm:A00.9 . } }")
        self.assertIn(f"?c rdf:type icd-10-gm:A00.9 {INF}", q)
        self.assertIn("PREFIX icd-10-gm: <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/>", q)

    def test_dotted_codes_are_not_split(self):
        q = expand("SELECT * WHERE { ?c a chop:00.0 . ?d a icd-10-gm:A00.0 }")
        self.assertIn(f"chop:00.0 {INF}", q)
        self.assertIn(f"icd-10-gm:A00.0 {INF}", q)
        self.assertNotIn("A00 OPTION", q)

    def test_inference_forced_on_versioned_systems_only(self):
        q = expand("SELECT * WHERE { ?c a icd-10-gm:E11 . ?u a ucum:kg . ?x a sphn:Birth }")
        self.assertIn(f"icd-10-gm:E11 {INF}", q)
        self.assertNotIn(f"ucum:kg {INF}", q)
        self.assertNotIn(f"sphn:Birth {INF}", q)

    def test_option_before_semicolon_is_split(self):
        q = expand("SELECT * WHERE { ?c isa icd-10-gm:A00 ; a ?concept . }")
        self.assertIn(f"{INF} .", q)
        self.assertIn("?c a ?concept", q)

    def test_strings_and_comments_untouched(self):
        q = expand('SELECT ("isa icd-10-gm:X" AS ?s) WHERE { ?a ?b ?c } # isa snomed:1')
        self.assertIn('"isa icd-10-gm:X"', q)

    def test_round_trip(self):
        original = (f"PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\n"
                    f"PREFIX icd-10-gm: <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/>\n"
                    f"SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ ?d a sphn:BilledDiagnosis ; "
                    f"sphn:hasCode ?c . ?c a icd-10-gm:A00 {INF} . }} }}")
        c = compact(original)
        self.assertIn("?c isa icd-10-gm:A00", c)
        self.assertNotIn("PREFIX", c)
        self.assertEqual(body(expand(c)), body(original).replace(" a icd-10-gm:A00", " rdf:type icd-10-gm:A00"))

    def test_having_alias_inlined(self):
        q = expand("SELECT ?c (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?x a ?c } } GROUP BY ?c HAVING (?n >= 100)")
        self.assertIn("HAVING (COUNT(DISTINCT ?g) >= 100)", q)

    def test_empty_prefix_means_sphn(self):
        q = expand("SELECT * WHERE { ?x a :BilledDiagnosis ; :hasCode ?c }")
        self.assertIn("sphn:BilledDiagnosis", q)
        self.assertIn("sphn:hasCode", q)

    def test_extract_from_markdown(self):
        self.assertEqual(extract_query("Here:\n```sparql\nSELECT * WHERE {?s ?p ?o}\n```\nDone"),
                         "SELECT * WHERE {?s ?p ?o}")


class PrefixTests(unittest.TestCase):
    def test_only_needed_prefixes(self):
        q = add_prefixes("SELECT * WHERE { ?x a sphn:Birth ; sphn:hasDate ?d . FILTER(?d > 'a:b') }")
        self.assertIn("PREFIX sphn:", q)
        self.assertEqual(q.count("PREFIX"), 1)

    def test_used_prefixes_ignores_iris_and_strings(self):
        self.assertEqual(used_prefixes('SELECT * { <http://x/y> ?p "snomed:1" . ?x a geno:0000135 }'), {"geno"})


class ParserTests(unittest.TestCase):
    def test_triples_and_graphs(self):
        f = analyse("SELECT ?x WHERE { GRAPH ?g { ?d a sphn:BilledDiagnosis ; sphn:hasCode ?c . "
                    f"?c a icd-10-gm:A00 {INF} . }} OPTIONAL {{ GRAPH ?g {{ ?p sphn:hasRank ?r }} }} }}")
        preds = {(t.s, t.p, t.o, t.graph, t.optional_depth, t.inference) for t in f.triples}
        self.assertIn(("?d", "rdf:type", "sphn:BilledDiagnosis", "?g", 0, False), preds)
        self.assertIn(("?c", "rdf:type", "icd-10-gm:A00", "?g", 0, True), preds)
        self.assertIn(("?p", "sphn:hasRank", "?r", "?g", 1, False), preds)

    def test_exists_inside_bind(self):
        f = analyse('SELECT * WHERE { GRAPH ?g { ?a a sphn:X } BIND(IF(EXISTS { GRAPH ?g { ?b a sphn:Y } }, 1, 0) AS ?f) }')
        self.assertIn("sphn:Y", {t.o for t in f.triples})


class SafetyTests(unittest.TestCase):
    def test_updates_and_federation_never_executed(self):
        for q in ("DELETE WHERE { ?s ?p ?o }", "LOAD <http://x>", "CLEAR GRAPH <urn:x>",
                  "SELECT * { SERVICE <http://x> { ?s ?p ?o } }", 'DEFINE get:soft "replace" SELECT * {?s ?p ?o}',
                  'SELECT (bif:file_to_string("/etc/passwd") AS ?x) {}'):
            self.assertFalse(is_safe_to_execute(q), q)
        for q in ("SELECT ?add ?load WHERE { ?s ?p ?add }", 'SELECT ("drop" AS ?x) {}'):
            self.assertTrue(is_safe_to_execute(q), q)


def result(vars_, rows):
    def cell(v):
        if isinstance(v, (int, float)):
            return {"type": "literal", "datatype": "http://www.w3.org/2001/XMLSchema#integer", "value": str(v)}
        if str(v).startswith("http"):
            return {"type": "uri", "value": v}
        return {"type": "literal", "value": v}
    return QueryResult(True, vars_, [{k: cell(v) for k, v in zip(vars_, r)} for r in rows], None, 0.0)


class CompareTests(unittest.TestCase):
    def test_column_names_and_order_do_not_matter(self):
        a = result(["sex", "n"], [["http://snomed.info/id/1", 5], ["http://snomed.info/id/2", 7]])
        b = result(["count", "s"], [[7, "http://snomed.info/id/2"], [5, "http://snomed.info/id/1"]])
        self.assertTrue(compare(a, b)["strict"])

    def test_relaxed_and_projection(self):
        ref = result(["y", "n"], [[2001, 5], [2002, 7]])
        more = result(["y", "n", "p"], [[2001, 5, 1], [2002, 7, 2]])
        less = result(["y"], [[2001], [2002]])
        self.assertFalse(compare(ref, more)["strict"])
        self.assertTrue(compare(ref, more)["relaxed"])
        self.assertTrue(compare(ref, less)["projection"])

    def test_numeric_ignores_label_presentation(self):
        a = result(["sex", "n"], [["http://snomed.info/id/248153007", 5], ["http://snomed.info/id/248152002", 7]])
        b = result(["sex", "n"], [["male", 5], ["female", 7]])
        r = compare(a, b)
        self.assertFalse(r["strict"])
        self.assertTrue(r["numeric"])

    def test_different_counts_never_match(self):
        a = result(["n"], [[5]])
        b = result(["n"], [[6]])
        self.assertFalse(any(compare(a, b).values()))


if __name__ == "__main__":
    unittest.main()
