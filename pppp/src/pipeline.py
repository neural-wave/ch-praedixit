"""Translation of one natural-language request into SPARQL, with its compliance verdict.

A request goes through three steps:
- Gate A (pppp.checker.GateA) reads the request text and estimates whether it asks for something
  the rules forbid;
- the generator (nl2sparql-generator/runtime) translates the request into a SPARQL query;
- Gate B (pppp.checker.GateB) checks the query against the rules, and schema_check looks for
  classes and properties the patient data does not contain.

When verdict.should_retry_translation says so, the generator writes the query again, told the
blocking problems its own validation found in the previous query (GenerationResult.blocking_problems).
verdict.final_verdict then combines the Gate A decision with the Gate B violations of the last query.

Usage: python -m pppp.pipeline "How many patients have a billed diagnosis of cholera?"
"""

import dataclasses
import json
import sys

from pppp.checker.GateA.gate import IntentGate
from pppp.checker.GateB import check_query
from pppp.checker.schema_check import schema_problems
from pppp.checker.verdict import final_verdict, should_retry_translation
from pppp.config import REPOSITORY_ROOT, load_settings

GENERATOR_RUNTIME_DIRECTORY = REPOSITORY_ROOT / "nl2sparql-generator" / "runtime"


@dataclasses.dataclass(frozen=True)
class PipelineResult:
    """The answer to one request, with the fields of the /nl-to-sparql response.

    sparql_query: the last query the generator wrote;
    tokens_consumed: tokens read by Gate A plus the prompt and completion tokens of every
        generator call, retries included;
    is_compliant, compliance_reason: from verdict.final_verdict; compliance_reason is None
        when the query is compliant.
    """

    sparql_query: str
    tokens_consumed: int
    is_compliant: bool
    compliance_reason: str = None


def load_generator(generator_config_path):
    """Return the NL-to-SPARQL Generator configured by the config.json at `generator_config_path`."""
    # The generator is not an installed package: its runtime directory is made importable.
    if str(GENERATOR_RUNTIME_DIRECTORY) not in sys.path:
        sys.path.insert(0, str(GENERATOR_RUNTIME_DIRECTORY))
    from nl2sparql.generator import Generator, load_config

    return Generator(load_config(generator_config_path))


class Pipeline:
    """Gate A, the generator and Gate B, loaded once and run on every request."""

    def __init__(self, intent_gate, generator, max_retries):
        """`intent_gate` is a GateA IntentGate, `generator` an nl2sparql Generator."""
        self.intent_gate = intent_gate
        self.generator = generator
        self.max_retries = max_retries

    @classmethod
    def load(cls, settings=None):
        """Return a pipeline with the models named in `settings` (a PipelineSettings; default: config/config.yaml)."""
        if settings is None:
            settings = load_settings()
        intent_gate = IntentGate.load(settings.gate_a_model_directory)
        generator = load_generator(settings.generator_config_path)
        return cls(intent_gate, generator, settings.max_retries)

    def run(self, request_text):
        """Translate one request and judge its compliance; return a PipelineResult.

        Errors of the models (e.g. the LLM server not answering) are raised unchanged.
        """
        gate_a_decision = self.intent_gate.decide(request_text)
        generation = self.generator.generate(request_text)
        tokens_consumed = gate_a_decision.tokens_used + generation.usage["total_tokens"]
        gate_b_violations = check_query(generation.sparql)
        query_schema_problems = schema_problems(generation.sparql)

        retries_done = 0
        while should_retry_translation(gate_a_decision, gate_b_violations, retries_done,
                                       self.max_retries, query_schema_problems):
            # With no problem to report, the model has nothing to fix and would likely write the same query again.
            if not generation.blocking_problems:
                break
            generation = self.generator.revise(generation)
            tokens_consumed += generation.usage["total_tokens"]
            gate_b_violations = check_query(generation.sparql)
            query_schema_problems = schema_problems(generation.sparql)
            retries_done += 1

        verdict = final_verdict(gate_a_decision, gate_b_violations)
        return PipelineResult(
            sparql_query=generation.sparql,
            tokens_consumed=tokens_consumed,
            is_compliant=verdict.is_compliant,
            compliance_reason=verdict.reason,
        )


def main():
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[-1])
        sys.exit(1)
    pipeline = Pipeline.load()
    pipeline_result = pipeline.run(sys.argv[1])
    print(json.dumps(dataclasses.asdict(pipeline_result), indent=2))


if __name__ == "__main__":
    main()
