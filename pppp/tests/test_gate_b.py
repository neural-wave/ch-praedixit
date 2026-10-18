"""Regression tests of Gate B: the per-rule examples and the labelled evaluation sets.

    python -m pytest pppp/tests                      # everything
    python -m pytest pppp/tests -k small_group       # one rule's examples
    python -m pytest pppp/tests -k evaluation_set    # one evaluation set

- gate_b_examples/<rule>/block/*.rq must be reported with <rule>;
  gate_b_examples/<rule>/pass/*.rq must get no violation at all.
- Every rule of rules.Rule has a folder with at least one block example (see
  gate_b_examples/README.md for how to add a rule).
- Every entry of the labelled sets with `# expected_compliant: true` must get no violation, and
  every entry with `false` at least one; `ambiguous` entries are skipped.

A failing test prints what explain shows for the query: run
`python -m pppp.checker.GateB.explain <file>` to see it in full.
"""

import pathlib
import re

import pytest

from pppp.checker.GateB import check_query
from pppp.checker.GateB.explain import explain
from pppp.checker.GateB.rules import Rule

TESTS_DIRECTORY = pathlib.Path(__file__).resolve().parent
EXAMPLES_DIRECTORY = TESTS_DIRECTORY / "gate_b_examples"
# This file is at <repository>/pppp/tests/test_gate_b.py.
REPOSITORY_ROOT = TESTS_DIRECTORY.parents[1]
LABELLED_SETS = {
    "evaluation_set": REPOSITORY_ROOT / "data" / "evaluation_set",
    "test_queries": REPOSITORY_ROOT / "data" / "test-queries",
    "evaluation_marzio": REPOSITORY_ROOT / "data" / "evaluation_marzio" / "compliance_checker" / "rq_files",
}
EXPECTED_COMPLIANT = re.compile(r"^# expected_compliant: (\w+)", re.MULTILINE)


def example_cases():
    cases = []
    for path in sorted(EXAMPLES_DIRECTORY.glob("*/*/*.rq")):
        rule, kind = path.parts[-3], path.parts[-2]
        cases.append(pytest.param(path, rule, kind, id=f"{rule}/{kind}/{path.stem}"))
    return cases


def labelled_cases():
    cases = []
    for set_name, directory in LABELLED_SETS.items():
        for path in sorted(directory.rglob("*.rq")):
            expected = EXPECTED_COMPLIANT.search(path.read_text(encoding="utf-8"))
            if expected is None or expected.group(1) not in ("true", "false"):
                continue
            case_id = f"{set_name}/{path.relative_to(directory)}"
            cases.append(pytest.param(path, expected.group(1) == "true", id=case_id))
    return cases


def failure_message(path, query_text, only_violations):
    """The explanation of a failing query: in full when a violation was missed (the labels tell
    why), only the violations when the query was wrongly rejected."""
    return f"\n{path}\n\n{explain(query_text, only_violations=only_violations)}"


@pytest.mark.parametrize("path, rule, kind", example_cases())
def test_rule_example(path, rule, kind):
    query_text = path.read_text(encoding="utf-8")
    reported_rules = {violation.rule.value for violation in check_query(query_text)}
    if kind == "block":
        assert rule in reported_rules, failure_message(path, query_text, only_violations=False)
    else:
        assert not reported_rules, failure_message(path, query_text, only_violations=True)


def test_every_rule_has_examples():
    """A rule without block examples is not tested: add gate_b_examples/<rule>/block/*.rq."""
    missing = []
    for rule in Rule:
        if rule is Rule.NONE:
            continue
        if not list((EXAMPLES_DIRECTORY / rule.value / "block").glob("*.rq")):
            missing.append(rule.value)
    assert not missing, f"rules without block examples: {', '.join(missing)}"


def test_example_folders_are_rules():
    """A folder name that is not a rule value (a typo) would silently test nothing."""
    rule_values = {rule.value for rule in Rule}
    unknown = sorted(path.name for path in EXAMPLES_DIRECTORY.iterdir() if path.is_dir() and path.name not in rule_values)
    assert not unknown, f"example folders that are not rules: {', '.join(unknown)}"


@pytest.mark.parametrize("path, expected_compliant", labelled_cases())
def test_labelled_entry(path, expected_compliant):
    query_text = path.read_text(encoding="utf-8")
    violations = check_query(query_text)
    if expected_compliant:
        assert not violations, "wrongly rejected" + failure_message(path, query_text, only_violations=True)
    else:
        assert violations, "not caught" + failure_message(path, query_text, only_violations=False)
