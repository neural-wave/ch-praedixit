"""Tests of the schema check (pppp/src/checker/schema_check.py) against data/sphn/data_schema.json."""

import pytest

from pppp.checker.schema_check import schema_problems

PREFIXES = """PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX icd-10-gm: <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/>
"""
COUNT = "SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE "


def problems_of(body):
    return schema_problems(PREFIXES + COUNT + body)


@pytest.mark.parametrize("body", [
    "{ GRAPH ?g { ?d a sphn:BilledDiagnosis ; sphn:hasCode ?c . ?c a icd-10-gm:E11 . } }",
    # 2024.2 properties the data uses, though schema 2026.1 does not define them
    "{ GRAPH ?g { ?case a sphn:AdministrativeCase ; sphn:hasAdmissionDateTime ?admission . } }",
    "{ GRAPH ?g { ?d a sphn:BilledDiagnosis ; sphn:hasRank ?rank . } }",
    # a property path through records of other classes
    "{ GRAPH ?g { ?d a sphn:BilledDiagnosis ; sphn:hasSubjectAge/sphn:hasQuantity/sphn:hasValue ?age . } }",
    # no class given: only the property's existence is checked
    "{ GRAPH ?g { ?x sphn:hasCode ?c . } }",
    # the terminology graph is not patient data
    "{ GRAPH ?g { ?d a sphn:BilledDiagnosis ; sphn:hasCode ?c . ?c a ?cls . } "
    "GRAPH <https://biomedit.ch/rdf/sphn-resource/global> { ?cls rdfs:subClassOf* icd-10-gm:E11 } }",
], ids=["billed-diagnosis-code", "admission-2024-2", "rank-2024-2", "age-path", "no-class", "terminology-graph"])
def test_query_that_fits_the_data(body):
    assert problems_of(body) == []


def test_unknown_property():
    problems = problems_of("{ GRAPH ?g { ?d a sphn:BilledDiagnosis ; sphn:hasMadeUpProperty ?x . } }")
    assert [problem.term for problem in problems] == ["sphn:hasMadeUpProperty"]


def test_misspelled_property_gets_a_suggestion():
    problems = problems_of("{ GRAPH ?g { ?d a sphn:BilledDiagnosis ; sphn:hasRecordDatetime ?t . } }")
    assert len(problems) == 1
    assert "sphn:hasRecordDateTime" in problems[0].message


def test_property_on_the_wrong_class_lists_the_right_ones():
    problems = problems_of("{ GRAPH ?g { ?d a sphn:BilledDiagnosis ; sphn:hasMethodCode ?m . } }")
    assert [problem.term for problem in problems] == ["?d sphn:hasMethodCode"]
    assert "sphn:hasCode" in problems[0].message and "sphn:hasRecordDateTime" in problems[0].message


def test_unknown_class():
    problems = problems_of("{ GRAPH ?g { ?d a sphn:NoSuchDiagnosis ; sphn:hasCode ?c . } }")
    assert [problem.term for problem in problems] == ["sphn:NoSuchDiagnosis"]


def test_bad_step_inside_a_path():
    problems = problems_of("{ GRAPH ?g { ?d a sphn:BilledDiagnosis ; sphn:hasSubjectAge/sphn:hasQuantitty/sphn:hasValue ?age . } }")
    assert [problem.term for problem in problems] == ["sphn:hasQuantitty"]


def test_invalid_query_is_left_to_gate_b():
    assert schema_problems(PREFIXES + "SELECT ?x WHERE { GRAPH ?g { ?x a sphn:BilledDiagnosis }") == []
