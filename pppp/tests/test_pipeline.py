"""Tests of the request pipeline (pppp/src/pipeline.py) with a fake Gate A and a fake generator.

Gate B and schema_check are the real ones: the fake generator returns fixed queries.
"""

import dataclasses

from pppp.checker.GateA.gate import GateADecision
from pppp.pipeline import Pipeline

PREFIX = "PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>\nPREFIX snomed: <http://snomed.info/id/>\n"
GROUPS = ("?sex a sphn:AdministrativeSex ; sphn:hasCode ?sc . ?sc a snomed:248153007 . "
          "?nat a sphn:Nationality ; sphn:hasAssociatedCountry/sphn:hasCode ?nc . ?nc a snomed:223369002 . "
          "?b a sphn:BirthDate ; sphn:hasYear ?year .")
# Compliant: patients born in a decade.
COMPLIANT_QUERY = PREFIX + f"SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ {GROUPS} }} FILTER (?year >= 1970 && ?year < 1980) }}"
# Non-compliant: patients born in a single year, a group small enough to identify them.
NON_COMPLIANT_QUERY = PREFIX + f"SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ {GROUPS} }} FILTER (?year = 1975) }}"
# Not valid SPARQL: a closing brace is missing.
INVALID_QUERY = PREFIX + "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?d a sphn:BilledDiagnosis }"

GATE_A_TOKENS = 12
GENERATION_TOKENS = 1000
# Blocking problems as the generator's validation reports them (GenerationResult.blocking_problems).
UNBALANCED = [{"severity": "error", "code": "unbalanced", "message": "Parentheses are unbalanced."}]
NO_PROBLEMS = []


def gate_a_decision(refused):
    """Return a GateADecision that refuses or accepts the request."""
    if refused:
        return GateADecision(refused=True, non_compliant_probability=0.95, rule="single_patient",
                             answer={}, tokens_used=GATE_A_TOKENS)
    return GateADecision(refused=False, non_compliant_probability=0.05, rule="none",
                         answer={}, tokens_used=GATE_A_TOKENS)


class FakeIntentGate:
    """Gives the same decision for every request."""

    def __init__(self, refused):
        self.refused = refused

    def decide(self, request_text):
        return gate_a_decision(self.refused)


@dataclasses.dataclass
class FakeGenerationResult:
    """The fields the pipeline reads from an nl2sparql GenerationResult."""

    sparql: str
    blocking_problems: list
    usage: dict


class FakeGenerator:
    """Returns the generations of `generations_in_order` one per call (generate first, then each revise).

    Each generation is a (query, blocking problems) pair.
    """

    def __init__(self, generations_in_order):
        self.generations_in_order = list(generations_in_order)
        self.revised_results = []

    def next_result(self):
        query, blocking_problems = self.generations_in_order.pop(0)
        return FakeGenerationResult(sparql=query, blocking_problems=blocking_problems,
                                    usage={"total_tokens": GENERATION_TOKENS})

    def generate(self, request_text):
        return self.next_result()

    def revise(self, previous):
        self.revised_results.append(previous)
        return self.next_result()


def run_pipeline(refused, generations_in_order, max_retries=1):
    """Run a pipeline with fakes on one request; return (PipelineResult, the FakeGenerator)."""
    generator = FakeGenerator(generations_in_order)
    pipeline = Pipeline(FakeIntentGate(refused), generator, max_retries)
    return pipeline.run("How many patients ...?"), generator


def test_a_compliant_query_is_returned_without_retry():
    pipeline_result, generator = run_pipeline(refused=False, generations_in_order=[(COMPLIANT_QUERY, NO_PROBLEMS)])
    assert pipeline_result.sparql_query == COMPLIANT_QUERY
    assert pipeline_result.is_compliant and pipeline_result.compliance_reason is None
    assert pipeline_result.tokens_consumed == GATE_A_TOKENS + GENERATION_TOKENS
    assert generator.revised_results == []


def test_an_invalid_query_with_generator_problems_is_revised():
    pipeline_result, generator = run_pipeline(refused=False,
                                              generations_in_order=[(INVALID_QUERY, UNBALANCED),
                                                                    (COMPLIANT_QUERY, NO_PROBLEMS)])
    assert pipeline_result.sparql_query == COMPLIANT_QUERY and pipeline_result.is_compliant
    assert pipeline_result.tokens_consumed == GATE_A_TOKENS + 2 * GENERATION_TOKENS
    assert len(generator.revised_results) == 1
    assert generator.revised_results[0].blocking_problems == UNBALANCED


def test_no_retry_when_the_generator_has_no_problem_to_report():
    pipeline_result, generator = run_pipeline(refused=False, generations_in_order=[(NON_COMPLIANT_QUERY, NO_PROBLEMS)])
    assert not pipeline_result.is_compliant and pipeline_result.compliance_reason
    assert generator.revised_results == []
    assert pipeline_result.tokens_consumed == GATE_A_TOKENS + GENERATION_TOKENS


def test_no_retry_when_both_gates_object():
    pipeline_result, generator = run_pipeline(refused=True, generations_in_order=[(NON_COMPLIANT_QUERY, UNBALANCED)])
    assert not pipeline_result.is_compliant and pipeline_result.compliance_reason
    assert generator.revised_results == []


def test_retries_stop_at_max_retries():
    pipeline_result, generator = run_pipeline(refused=False,
                                              generations_in_order=[(INVALID_QUERY, UNBALANCED)] * 3,
                                              max_retries=2)
    assert len(generator.revised_results) == 2
    assert not pipeline_result.is_compliant
