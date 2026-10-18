"""REST API: POST /nl-to-sparql translates a natural-language request into SPARQL.

Request and response follow the hackathon API_specification.md:
- request body: {"text": "<natural-language request>"};
- 200: {"sparql_query", "tokens_consumed", "is_compliant"}, plus "compliance_reason" only when the
  query is not compliant (a non-compliant query is still a successful translation);
- any error (bad input, translation failure): {"error": "<message>"}.

The translation itself is done by pppp.pipeline.Pipeline (Gate A, generator, Gate B).
"""

import logging
import threading

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

EMPTY_TEXT_ERROR = "'text' must not be empty."

logger = logging.getLogger(__name__)


class TranslationRequest(BaseModel):
    """The body of POST /nl-to-sparql."""

    text: str


def error_response(status_code, message):
    """Return a JSONResponse with the error body of the specification: {"error": message}."""
    return JSONResponse(status_code=status_code, content={"error": message})


def validation_error_message(validation_error):
    """Return one readable line for the first problem of a RequestValidationError.

    Example: "Invalid request body, 'text': Field required".
    """
    first_problem = validation_error.errors()[0]
    # The location starts with "body"; the rest is the path of the field, if any.
    field_names = []
    for location_part in first_problem["loc"]:
        if location_part == "body":
            continue
        field_names.append(str(location_part))
    if not field_names:
        return f"Invalid request body: {first_problem['msg']}"
    return f"Invalid request body, '{'.'.join(field_names)}': {first_problem['msg']}"


def create_app(pipeline):
    """Return the FastAPI application serving POST /nl-to-sparql with `pipeline` (a pppp.pipeline.Pipeline)."""
    app = FastAPI(title="Natural Language to SPARQL API", version="1.0.0")
    # FastAPI runs each request of a plain `def` endpoint in its own thread, and the Gate A
    # tokenizer fails when two threads use it at once: requests go through the pipeline one at a time.
    pipeline_lock = threading.Lock()

    @app.exception_handler(RequestValidationError)
    def invalid_request_handler(request, validation_error):
        # FastAPI would answer 422 {"detail": [...]}; the specification wants {"error": "..."}.
        return error_response(400, validation_error_message(validation_error))

    @app.post("/nl-to-sparql")
    def nl_to_sparql(translation_request: TranslationRequest):
        request_text = translation_request.text.strip()
        if not request_text:
            return error_response(400, EMPTY_TEXT_ERROR)

        try:
            with pipeline_lock:
                pipeline_result = pipeline.run(request_text)
        except Exception as pipeline_error:
            logger.exception("Translation failed for request %r", request_text)
            return error_response(500, f"Translation failed: {pipeline_error}")

        response_body = {
            "sparql_query": pipeline_result.sparql_query,
            "tokens_consumed": pipeline_result.tokens_consumed,
            "is_compliant": pipeline_result.is_compliant,
        }
        if not pipeline_result.is_compliant:
            response_body["compliance_reason"] = pipeline_result.compliance_reason
        return response_body

    return app
