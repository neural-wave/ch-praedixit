"""Structural compliance check (Gate B) of a SPARQL request.

`check_query` runs the checks layer by layer and returns every violation it finds. An empty
list means that no certain violation was found, not that the query is compliant: the query
still goes to the next gate.

The checks are listed in CHECKS, in the order they run. `run_checks` runs them and keeps the
result of each one, which `pppp.checker.GateB.explain` prints; `check_query` returns them all
as one list.

`rules` chooses the rules to enforce (rules.RuleSet): "extended" (the default) runs every check;
"base" keeps only the rules of Query_Compliance.md. It skips the checks that exist only for the
new rules of RULES.md, and reports the new rules that rename what a base check found under the
original rule (rules.BASE_RULE_OF), e.g. GROUP BY a patient as not_aggregated.
"""

import dataclasses
import functools

from pppp.checker.GateB.form_checks import (
    dataset_violations,
    function_violations,
    query_form_violations,
    service_violations,
)
from pppp.checker.GateB.output_checks import exact_date_group_violations, output_violations
from pppp.checker.GateB.parsing import parse_request
from pppp.checker.GateB.pattern_checks import constant_iri_violations, predicate_violations
from pppp.checker.GateB.rules import BASE_RULE_OF, Rule, RuleSet, Violation
from pppp.checker.GateB.sensitivity_checks import (
    FixedValues,
    code_identifier_comparison_violations,
    exact_date_violations,
    identifier_literal_violations,
    limited_subquery_violations,
    quasi_identifier_violations,
    sensitive_comparison_violations,
    small_group_violations,
)
from pppp.checker.GateB.variable_labels import variable_labels
from pppp.checker.GateB.virtuoso_extensions import scan_virtuoso_extensions
from pppp.checker.GateB.vocabulary import load_vocabulary


class QueryContext:
    """What the checks of one query share: its algebra and the facts computed from it once."""

    def __init__(self, algebra_root):
        self.algebra_root = algebra_root
        self.vocabulary = load_vocabulary()

    @functools.cached_property
    def labels(self):
        """The QueryLabels of the query's variables (see variable_labels)."""
        return variable_labels(self.algebra_root, self.vocabulary)

    @functools.cached_property
    def fixed_values(self):
        """What the query's filters and VALUES fix (see sensitivity_checks.FixedValues)."""
        return FixedValues(self.algebra_root)


@dataclasses.dataclass(frozen=True)
class Check:
    """One structural check: its layer, a name, what it looks for, how to run it on a QueryContext,
    and the rule set that needs it (RuleSet.EXTENDED: only for the new rules of RULES.md)."""

    layer: str
    name: str
    looks_for: str
    run: object
    rule_set: RuleSet = RuleSet.BASE


LAYER_1 = "1. query form, dataset, federation, functions"
LAYER_2 = "2. triple patterns and constant IRIs"
LAYER_3 = "3. patients selected through values"
LAYER_4 = "4. what the outer query returns"

CHECKS = [
    Check(LAYER_1, "query form", "a query that is not a SELECT",
          lambda context: query_form_violations(context.algebra_root)),
    Check(LAYER_1, "dataset clauses", "FROM / FROM NAMED clauses",
          lambda context: dataset_violations(context.algebra_root)),
    Check(LAYER_1, "federation", "SERVICE calls to other endpoints",
          lambda context: service_violations(context.algebra_root)),
    Check(LAYER_1, "functions", "function calls outside the allow list",
          lambda context: function_violations(context.algebra_root)),
    Check(LAYER_2, "unrestricted patterns", "patterns that match the whole graph (?s ?p ?o, ?s a ?c, !p, p*)",
          lambda context: predicate_violations(context.algebra_root)),
    Check(LAYER_2, "constant IRIs", "patient graph or record IRIs written in the query",
          lambda context: constant_iri_violations(context.algebra_root)),
    Check(LAYER_3, "sensitive comparisons", "a SENSITIVE value compared with a constant (=, !=, CONTAINS...)",
          lambda context: sensitive_comparison_violations(context.algebra_root, context.labels)),
    Check(LAYER_3, "identifier literals", "an identifier property fixed to a literal in a triple pattern",
          lambda context: identifier_literal_violations(context.algebra_root, context.vocabulary)),
    Check(LAYER_3, "code identifiers", "a code identifier compared with a per-patient ID (ID-<digits>)",
          lambda context: code_identifier_comparison_violations(context.algebra_root, context.vocabulary)),
    Check(LAYER_3, "cut subqueries", "a subquery cut by LIMIT that picks specific patients or records",
          lambda context: limited_subquery_violations(context.algebra_root, context.labels)),
    Check(LAYER_3, "small groups", "a HAVING (or filter on a count) keeping groups of 5 patients or fewer",
          lambda context: small_group_violations(context.algebra_root), RuleSet.EXTENDED),
    Check(LAYER_3, "exact dates", "an exact timestamp or a full date fixed in a pattern or filter",
          lambda context: exact_date_violations(context.algebra_root, context.fixed_values), RuleSet.EXTENDED),
    Check(LAYER_3, "identifying attributes", "3 or more identifying attributes fixed to exact values",
          lambda context: quasi_identifier_violations(context.algebra_root, context.fixed_values), RuleSet.EXTENDED),
    Check(LAYER_4, "outputs", "returned values that are not aggregated, expose identifiers or group too finely",
          lambda context: output_violations(context.algebra_root, context.labels)),
    Check(LAYER_4, "exact-date groups", "results grouped by year, month and day of the same date",
          lambda context: exact_date_group_violations(context.algebra_root), RuleSet.EXTENDED),
]


@dataclasses.dataclass
class CheckRun:
    """Everything a run of Gate B found on one request.

    extension_violations: Virtuoso extensions (DEFINE, OPTION) that are not allowed;
    parse_violation: NOT_READ_ONLY for an update, INVALID_SPARQL for invalid input, else None;
    context: the QueryContext of a parsed query, else None;
    results: [(Check, list of Violation)] in the order the checks ran;
    rules: the RuleSet enforced; skipped: the checks it does not need (base rules only).
    """

    extension_violations: list
    parse_violation: Violation = None
    context: QueryContext = None
    results: list = dataclasses.field(default_factory=list)
    rules: RuleSet = RuleSet.EXTENDED
    skipped: list = dataclasses.field(default_factory=list)

    def violations(self):
        """Return every violation, in the order the checks found them."""
        found = list(self.extension_violations)
        if self.parse_violation is not None:
            found.append(self.parse_violation)
        for check, check_violations in self.results:
            found.extend(check_violations)
        return found


def as_base_rule(violation):
    """Return the violation under the original rule it refines (rules.BASE_RULE_OF), or unchanged."""
    if violation.rule in BASE_RULE_OF:
        return Violation(BASE_RULE_OF[violation.rule], violation.reason)
    return violation


def run_checks(query_text, rules=RuleSet.EXTENDED):
    """Run the checks of CHECKS that `rules` needs ("base" or "extended") and return the CheckRun."""
    rules = RuleSet(rules)
    extension_scan = scan_virtuoso_extensions(query_text)
    run = CheckRun(extension_violations=list(extension_scan.violations), rules=rules)

    parsed_request = parse_request(extension_scan.standard_text)
    if parsed_request.is_update:
        reason = "The request modifies data (SPARQL update); only read-only queries are allowed."
        run.parse_violation = Violation(Rule.NOT_READ_ONLY, reason)
        return run
    if parsed_request.algebra is None:
        reason = f"The request is not valid SPARQL: {parsed_request.error}"
        run.parse_violation = Violation(Rule.INVALID_SPARQL, reason)
        return run

    run.context = QueryContext(parsed_request.algebra)
    for check in CHECKS:
        if rules == RuleSet.BASE and check.rule_set == RuleSet.EXTENDED:
            run.skipped.append(check)
            continue
        check_violations = check.run(run.context)
        if rules == RuleSet.BASE:
            check_violations = [as_base_rule(violation) for violation in check_violations]
        run.results.append((check, check_violations))
    return run


def check_query(query_text, rules=RuleSet.EXTENDED):
    """Return the list of Violation found in a SPARQL request (empty when none is certain).

    `rules` = "extended" (default) enforces every rule; "base" only those of Query_Compliance.md.
    """
    return run_checks(query_text, rules).violations()
