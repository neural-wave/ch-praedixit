# Compliance check: how to run it

The compliance check puts guardrails around the translator:

- **Gate A** (`GateA/`) reads the question before any query is written;
- **Gate B** (`GateB/`) reads the query the translator wrote and checks it against the rules;
- **the schema check** (`schema_check.py`) compares the query with the schema the patient data uses;
- **the verdict** (`verdict.py`) combines them and decides whether to translate again.

Run every command below from the repository root.

## Run the tests

```bash
python -m pytest pppp/tests                      # everything: rule examples, labelled sets, verdict, schema check
python -m pytest pppp/tests -k small_group       # one rule's examples
python -m pytest pppp/tests/test_rule_sets.py    # base vs extended rules
```

## Gate B on one query

`explain` shows how Gate B labelled each value of the query, what every check found, and the verdict:

```bash
python -m pppp.checker.GateB.explain pppp/tests/gate_b_examples/small_group/block/01-having-one.rq
python -m pppp.checker.GateB.explain query.rq --only-violations     # only the checks that found something
python -m pppp.checker.GateB.explain query.rq --rules base          # only the rules of Query_Compliance.md
python -m pppp.checker.GateB.explain < query.rq                     # read the query from stdin
```

For example, the small-group example is rejected with every rule and allowed with the base rules:

```
RULES  extended
    VIOLATION  small groups: small_group: The query keeps only groups of at most 1 patients ...
VERDICT  non-compliant (small_group)

RULES  base
VERDICT  no violation found (compliant for Gate B)
```

## Gate B on a labelled test set

```bash
python -m pppp.checker.gate_b_checker                                   # data/evaluation_set, every rule
python -m pppp.checker.gate_b_checker --rules base                      # the same, base rules only
python -m pppp.checker.gate_b_checker data/evaluation_marzio/compliance_checker/rq_files
```

It prints how many compliant entries are wrongly rejected and how many non-compliant ones are
caught, then lists the errors. With `--rules base`, the entries labelled by the new rules of
RULES.md show up as not caught.

## From Python

```python
from pppp.checker.GateB import check_query
from pppp.checker.schema_check import schema_problems
from pppp.checker.verdict import final_verdict, retry_feedback, should_retry_translation

query = """PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>
SELECT (COUNT(DISTINCT ?g) AS ?patients)
WHERE { GRAPH ?g { ?diagnosis a sphn:BilledDiagnosis ; sphn:hasMethodCode ?code . } }"""

violations = check_query(query)                      # Gate B with every rule ("extended", the default)
base_violations = check_query(query, rules="base")   # only the rules of Query_Compliance.md
problems = schema_problems(query)                    # translation mistakes; they never change the verdict

gate_a_decision = None                               # or IntentGate.load(...).decide(question), see below
verdict = final_verdict(gate_a_decision, violations)
print(verdict.is_compliant, verdict.reason)          # True None: the query is compliant

retries_done = 0
if should_retry_translation(gate_a_decision, violations, retries_done, schema_problems=problems):
    feedback = retry_feedback(violations, problems)  # give this to the translator with the same question
    print(feedback)
```

Here the query is compliant but uses a property billed diagnoses never carry, so it would
return nothing; the feedback reads:

```
Your previous query uses something the data does not contain. Records of sphn:BilledDiagnosis
never carry sphn:hasMethodCode; they carry sphn:hasAdministrativeCase, sphn:hasCode, ...
Write the corrected query for the same request.
```

A full request goes: Gate A on the question → translate → Gate B and the schema check on the
query → retry while `should_retry_translation` says so, passing `retry_feedback` → `final_verdict`.
Always translate, even when Gate A refuses: the API returns a query for every request, and Gate B
can overturn a refusal it cannot confirm.

## Gate A

Gate A needs a trained model (not in the repository):

```bash
python scripts/train/train_gate_a.py                                     # trains models/gate_a/ModernBERT-base
python scripts/testing/evaluate_gate_a.py models/gate_a/ModernBERT-base  # measures it on the test sets
python scripts/testing/evaluate_gate_a.py models/gate_a/ModernBERT-base --request "Which patients have type 2 diabetes?"
```

```python
from pppp.checker.GateA.gate import IntentGate

gate_a = IntentGate.load("models/gate_a/ModernBERT-base")
decision = gate_a.decide("Which patients have type 2 diabetes?")
print(decision.refused, decision.non_compliant_probability, decision.rule)
```

## Re-extract the data schema

The schema check reads `data/sphn/data_schema.json`. Rebuild it when the patient data changes:

```bash
python scripts/schema/extract_data_schema.py /path/to/hackathon-virtuoso-release/patients
```

## Add a Gate B rule

See `pppp/tests/gate_b_examples/README.md`: write the examples first, then the check, then
register it in `CHECKS` in `GateB/checker.py` (with `RuleSet.EXTENDED` if it is not a rule of
Query_Compliance.md).
