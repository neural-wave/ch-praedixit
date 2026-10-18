"""Tests of the REST API (pppp/src/api.py) with a fake pipeline: no model needed."""

from fastapi.testclient import TestClient

from pppp.api import EMPTY_TEXT_ERROR, create_app
from pppp.pipeline import PipelineResult

COMPLIANT_RESULT = PipelineResult(sparql_query="SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }",
                                  tokens_consumed=1287, is_compliant=True)
NON_COMPLIANT_RESULT = PipelineResult(sparql_query="DELETE WHERE { GRAPH ?g { ?d a ?type } }", tokens_consumed=412,
                                      is_compliant=False,
                                      compliance_reason="The query modifies data (DELETE); only read-only queries are allowed.")


class FakePipeline:
    """Returns `pipeline_result` for every request and remembers the request texts it received."""

    def __init__(self, pipeline_result):
        self.pipeline_result = pipeline_result
        self.received_texts = []

    def run(self, request_text):
        self.received_texts.append(request_text)
        return self.pipeline_result


class FailingPipeline:
    """Fails on every request, as the pipeline does when the LLM server does not answer."""

    def run(self, request_text):
        raise ConnectionError("LLM server not reachable")


def client_for(pipeline):
    return TestClient(create_app(pipeline))


def test_a_compliant_translation_has_no_compliance_reason():
    pipeline = FakePipeline(COMPLIANT_RESULT)
    response = client_for(pipeline).post("/nl-to-sparql", json={"text": "How many patients have diabetes?"})
    assert response.status_code == 200
    assert response.json() == {"sparql_query": COMPLIANT_RESULT.sparql_query, "tokens_consumed": 1287,
                               "is_compliant": True}
    assert pipeline.received_texts == ["How many patients have diabetes?"]


def test_a_non_compliant_translation_is_still_a_success_with_its_reason():
    response = client_for(FakePipeline(NON_COMPLIANT_RESULT)).post("/nl-to-sparql", json={"text": "Delete everything"})
    assert response.status_code == 200
    assert response.json() == {"sparql_query": NON_COMPLIANT_RESULT.sparql_query, "tokens_consumed": 412,
                               "is_compliant": False, "compliance_reason": NON_COMPLIANT_RESULT.compliance_reason}


def test_empty_or_blank_text_is_rejected():
    pipeline = FakePipeline(COMPLIANT_RESULT)
    client = client_for(pipeline)
    for blank_text in ["", "   "]:
        response = client.post("/nl-to-sparql", json={"text": blank_text})
        assert response.status_code == 400
        assert response.json() == {"error": EMPTY_TEXT_ERROR}
    assert pipeline.received_texts == []


def test_a_malformed_body_gets_an_error_body():
    client = client_for(FakePipeline(COMPLIANT_RESULT))
    missing_text = client.post("/nl-to-sparql", json={"question": "How many patients?"})
    text_not_a_string = client.post("/nl-to-sparql", json={"text": 42})
    not_json = client.post("/nl-to-sparql", content="How many patients?", headers={"Content-Type": "application/json"})
    for response in [missing_text, text_not_a_string, not_json]:
        assert response.status_code == 400
        assert list(response.json()) == ["error"]
    assert "'text'" in missing_text.json()["error"]


def test_a_pipeline_failure_gets_an_error_body():
    response = client_for(FailingPipeline()).post("/nl-to-sparql", json={"text": "How many patients?"})
    assert response.status_code == 500
    assert response.json() == {"error": "Translation failed: LLM server not reachable"}
