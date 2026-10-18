"""Tests of the final verdict and the retry decision (pppp/src/checker/verdict.py)."""

import dataclasses

import pytest

from pppp.checker.GateB import check_query
from pppp.checker.schema_check import SchemaProblem
from pppp.checker.verdict import final_verdict, retry_feedback, should_retry_translation


@dataclasses.dataclass(frozen=True)
class GateADecision:
    """The fields verdict.py reads from pppp.checker.GateA.gate.GateADecision (no model needed)."""

    refused: bool
    non_compliant_probability: float
    rule: str


ACCEPTED = GateADecision(refused=False, non_compliant_probability=0.1, rule="none")
REFUSED = GateADecision(refused=True, non_compliant_probability=0.95, rule="single_patient")
PREFIX = "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\nPREFIX snomed: <http://snomed.info/id/>\n"
GROUPS = ("?sex a sphn:AdministrativeSex ; sphn:hasCode ?sc . ?sc a snomed:248153007 . "
          "?nat a sphn:Nationality ; sphn:hasAssociatedCountry/sphn:hasCode ?nc . ?nc a snomed:223369002 . "
          "?b a sphn:BirthDate ; sphn:hasYear ?year .")
DECADE = check_query(PREFIX + f"SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ {GROUPS} }} FILTER (?year >= 1970 && ?year < 1980) }}")
YEAR_1975 = check_query(PREFIX + f"SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ {GROUPS} }} FILTER (?year = 1975) }}")
INVALID = check_query(PREFIX + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?d a sphn:BilledDiagnosis }")
SCHEMA_PROBLEM = SchemaProblem("sphn:hasMadeUpProperty", "No record in the data carries sphn:hasMadeUpProperty.")


def test_the_example_queries_get_the_expected_gate_b_verdicts():
    assert DECADE == [] and YEAR_1975 and INVALID[0].rule.value == "invalid_sparql"


@pytest.mark.parametrize("gate_a, violations, compliant, decided_by", [
    (ACCEPTED, DECADE, True, "both"),
    (REFUSED, DECADE, True, "gate_b"),         # Gate B overturns a refusal it cannot confirm
    (REFUSED, YEAR_1975, False, "both"),
    (ACCEPTED, YEAR_1975, False, "gate_b"),
    (None, YEAR_1975, False, "gate_b"),        # Gate A did not run
    (REFUSED, INVALID, False, "both"),
], ids=["both-pass", "overturned", "both-object", "only-gate-b", "no-gate-a", "invalid"])
def test_final_verdict(gate_a, violations, compliant, decided_by):
    verdict = final_verdict(gate_a, violations)
    assert (verdict.is_compliant, verdict.decided_by) == (compliant, decided_by)
    assert (verdict.reason is None) == compliant


def test_a_very_confident_refusal_can_stand_when_asked():
    confident = GateADecision(refused=True, non_compliant_probability=0.99, rule="single_patient")
    assert not final_verdict(confident, DECADE, gate_a_threshold=0.97).is_compliant
    assert final_verdict(REFUSED, DECADE, gate_a_threshold=0.97).is_compliant


def test_schema_problems_never_change_the_verdict():
    assert final_verdict(ACCEPTED, DECADE).is_compliant
    assert should_retry_translation(ACCEPTED, DECADE, retries_done=0, schema_problems=[SCHEMA_PROBLEM])


@pytest.mark.parametrize("gate_a, violations, problems, retries_done, expected", [
    (ACCEPTED, INVALID, [], 0, True),
    (REFUSED, INVALID, [], 0, True),           # invalid code is the translator's fault, whatever Gate A said
    (REFUSED, DECADE, [SCHEMA_PROBLEM], 0, True),
    (ACCEPTED, YEAR_1975, [], 0, True),        # Gate A saw no problem: the translation is suspect
    (REFUSED, YEAR_1975, [], 0, False),        # both object: the request is the problem
    (ACCEPTED, DECADE, [], 0, False),          # nothing to fix
    (REFUSED, INVALID, [], 1, False),          # retries used up
], ids=["invalid", "invalid-gate-a-refused", "schema-problem", "gate-b-only", "both-object", "nothing-wrong", "used-up"])
def test_should_retry_translation(gate_a, violations, problems, retries_done, expected):
    assert should_retry_translation(gate_a, violations, retries_done, schema_problems=problems) == expected


def test_retry_feedback_reports_parser_errors_and_schema_problems():
    feedback = retry_feedback(INVALID, [SCHEMA_PROBLEM])
    assert "not valid SPARQL" in feedback and INVALID[0].reason in feedback
    assert SCHEMA_PROBLEM.message in feedback


def test_retry_feedback_never_asks_to_fix_compliance():
    feedback = retry_feedback(YEAR_1975)
    assert "translate literally" in feedback and "do not soften" in feedback
    assert YEAR_1975[0].reason not in feedback


def test_no_feedback_when_nothing_is_wrong():
    assert retry_feedback([]) == ""
