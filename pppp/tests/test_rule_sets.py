"""Tests of Gate B's two rule sets: "base" (Query_Compliance.md only) and "extended" (base + RULES.md).

    python -m pytest pppp/tests/test_rule_sets.py
"""

import pathlib

import pytest

from pppp.checker.GateB import check_query
from pppp.checker.GateB.explain import explain
from pppp.checker.GateB.rules import BASE_RULE_OF, NEW_RULES, Rule

TESTS_DIRECTORY = pathlib.Path(__file__).resolve().parent
EXAMPLES_DIRECTORY = TESTS_DIRECTORY / "gate_b_examples"
REPOSITORY_ROOT = TESTS_DIRECTORY.parents[1]
LABELLED_DIRECTORIES = [
    REPOSITORY_ROOT / "data" / "evaluation_set",
    REPOSITORY_ROOT / "data" / "test-queries",
    REPOSITORY_ROOT / "data" / "evaluation_marzio" / "compliance_checker" / "rq_files",
]
NEW_RULE_VALUES = {rule.value for rule in NEW_RULES}
# New rules found only by checks of their own, which the base rules do not run.
OWN_CHECK_RULES = {rule.value for rule in NEW_RULES - set(BASE_RULE_OF)}


def all_queries():
    paths = sorted(EXAMPLES_DIRECTORY.glob("*/*/*.rq"))
    for directory in LABELLED_DIRECTORIES:
        paths.extend(sorted(directory.rglob("*.rq")))
    return paths


def rules_reported(path, rules):
    return {violation.rule.value for violation in check_query(path.read_text(encoding="utf-8"), rules=rules)}


def example_cases(folder_filter):
    cases = []
    for path in sorted(EXAMPLES_DIRECTORY.glob("*/block/*.rq")):
        rule = path.parts[-3]
        if folder_filter(rule):
            cases.append(pytest.param(path, rule, id=f"{rule}/{path.stem}"))
    return cases


def test_extended_is_the_default():
    query = (EXAMPLES_DIRECTORY / "small_group" / "block" / "01-having-one.rq").read_text(encoding="utf-8")
    assert check_query(query) == check_query(query, rules="extended")


def test_base_is_never_stricter_and_never_reports_a_new_rule():
    """Over every example and labelled query: base rejects nothing extended accepts, and names no new rule."""
    stricter, new_rule_named = [], []
    for path in all_queries():
        extended, base = rules_reported(path, "extended"), rules_reported(path, "base")
        if base and not extended:
            stricter.append(path.name)
        if base & NEW_RULE_VALUES:
            new_rule_named.append(path.name)
    assert not stricter, f"base rejects what extended accepts: {stricter}"
    assert not new_rule_named, f"base reports new rules: {new_rule_named}"


@pytest.mark.parametrize("path, rule", example_cases(lambda rule: rule not in NEW_RULE_VALUES))
def test_base_rules_block_the_same_examples(path, rule):
    assert rule in rules_reported(path, "base")


@pytest.mark.parametrize("path, rule", example_cases(lambda rule: rule in OWN_CHECK_RULES))
def test_new_rules_with_their_own_check_are_not_enforced_by_base(path, rule):
    assert not rules_reported(path, "base")


@pytest.mark.parametrize("path, rule", example_cases(lambda rule: rule in {r.value for r in BASE_RULE_OF}))
def test_renamed_rules_come_out_under_the_original_rule(path, rule):
    base = rules_reported(path, "base")
    if path.stem.endswith("year-month-day-functions"):
        assert not base  # grouping by year, month and day of one date is checked only by the extended rules
    else:
        assert BASE_RULE_OF[Rule(rule)].value in base


def test_explain_names_the_skipped_checks():
    query = (EXAMPLES_DIRECTORY / "small_group" / "block" / "01-having-one.rq").read_text(encoding="utf-8")
    text = explain(query, rules="base")
    assert "RULES  base" in text and "skipped" in text and "small groups" in text
    assert "no violation found" in text
