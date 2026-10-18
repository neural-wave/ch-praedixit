"""Layer 4 of the structural checks: what the outer query returns.

Uses the variable labels of pppp.checker.GateB.variable_labels. Subqueries may work patient
by patient; only the outer query (the top-level SELECT) decides what leaves the database:
- every returned variable must be an aggregate or a GROUP BY key;
- a GROUP BY key must be shared by many patients: a category, an aggregate computed in a
  subquery, or a bucket (YEAR, FLOOR...) of a record value. A SENSITIVE key gives one row
  per patient or record, a raw RECORD_VALUE key one row per record;
- MIN, MAX, SAMPLE and GROUP_CONCAT return values of their argument, so they must not run
  over a SENSITIVE value; SAMPLE and GROUP_CONCAT must not run over raw record values either.
  MIN and MAX of record values (e.g. the longest stay) are left to the next gate.
A SENSITIVE GROUP BY key is reported as ROW_PER_PATIENT, a raw RECORD_VALUE key, or the year,
month and day of the same date, as GROUPED_BY_EXACT_VALUE (RULES.md).
"""

from rdflib import Variable
from rdflib.plugins.sparql.parserutils import CompValue

from pppp.checker.GateB.algebra_walk import query_scopes, scope_nodes, variables_in_expression
from pppp.checker.GateB.rules import Rule, Violation
from pppp.checker.GateB.sensitivity_checks import (
    DATE_PART_FUNCTIONS,
    DATE_PART_PROPERTIES,
    FULL_DATE,
    last_property,
    patient_patterns,
    unwrapped,
)
from pppp.checker.GateB.variable_labels import Label

AGGREGATE_KEYWORDS = {
    "Aggregate_Count": "COUNT",
    "Aggregate_Sum": "SUM",
    "Aggregate_Avg": "AVG",
    "Aggregate_Min": "MIN",
    "Aggregate_Max": "MAX",
    "Aggregate_Sample": "SAMPLE",
    "Aggregate_GroupConcat": "GROUP_CONCAT",
}
VALUE_RETURNING_AGGREGATES = {"Aggregate_Min", "Aggregate_Max", "Aggregate_Sample", "Aggregate_GroupConcat"}
VALUE_LISTING_AGGREGATES = {"Aggregate_Sample", "Aggregate_GroupConcat"}
SENSITIVE_MEANING = "a patient graph, record or identifier"


def group_expressions(group_node):
    """Return the GROUP BY expressions of a Group node.

    rdflib turns an expression without alias (`GROUP BY (STR(?g))`, `GROUP BY (?d)`) into an
    Extend with no variable just below the Group and leaves None in the Group's list.
    """
    expressions = []
    has_unnamed_expression = False
    for group_expression in group_node.expr:
        if group_expression is None:
            has_unnamed_expression = True
        else:
            expressions.append(group_expression)
    if not has_unnamed_expression:
        return expressions
    node = group_node.p
    while node.name == "Extend":
        if node.var is None:
            expressions.append(node.expr)
        node = node.p
    return expressions


def outer_scope_facts(top_project):
    """Collect what the outer query computes.

    Returns (group_keys, explicit_aggregates, bind_expressions):
    - group_keys: {variable name: GROUP BY expression} of the outer GROUP BY;
    - explicit_aggregates: aggregate nodes written in the query, without the SAMPLE that
      rdflib adds to return each GROUP BY key;
    - bind_expressions: {variable name: expression} of the outer BIND and AS assignments.
    """
    group_keys = {}
    aggregates = []
    bind_expressions = {}
    for node in scope_nodes(top_project):
        if node.name == "Group" and node.expr is not None:
            for group_expression in group_expressions(node):
                for name in variables_in_expression(group_expression):
                    group_keys[name] = group_expression
        if node.name == "AggregateJoin":
            aggregates.extend(node.A)
        if node.name == "Extend":
            bind_expressions[str(node.var)] = node.expr

    explicit_aggregates = []
    for aggregate in aggregates:
        is_implicit_sample = (
            aggregate.name == "Aggregate_Sample"
            and isinstance(aggregate.vars, Variable)
            and str(aggregate.vars) in group_keys
        )
        if not is_implicit_sample:
            explicit_aggregates.append(aggregate)
    return group_keys, explicit_aggregates, bind_expressions


def sensitive_names(expression, query_labels):
    """Return the '?name' strings of the SENSITIVE variables used in an expression."""
    names = []
    for name in sorted(variables_in_expression(expression)):
        if query_labels.by_variable.get(name) == Label.SENSITIVE:
            names.append(f"?{name}")
    return names


def uses_explicit_aggregate(expression, explicit_aggregates):
    """Return True if an expression uses the result of one of the explicit aggregates."""
    explicit_result_names = set()
    for aggregate in explicit_aggregates:
        explicit_result_names.add(str(aggregate.res))
    for name in variables_in_expression(expression):
        if name in explicit_result_names:
            return True
    return False


def group_key_violations(group_keys, returned_names, query_labels, top_project):
    """Report GROUP BY keys that are SENSITIVE or raw record values."""
    violations = []
    for name, group_expression in sorted(group_keys.items()):
        label = query_labels.of_expression(group_expression, top_project)
        if label == Label.SENSITIVE:
            reason = f"The results are grouped by ?{name}, which holds {SENSITIVE_MEANING}: one row per patient or record."
            violations.append(Violation(Rule.ROW_PER_PATIENT, reason))
            if name in returned_names:
                reason = f"The query returns ?{name}, which holds {SENSITIVE_MEANING}."
                violations.append(Violation(Rule.EXPOSES_IDENTIFIER, reason))
        if label == Label.RECORD_VALUE:
            reason = (f"The results are grouped by ?{name}, a raw value of a patient record: each group is "
                      "a single record. Group by a category or a bucket (e.g. YEAR) instead.")
            violations.append(Violation(Rule.GROUPED_BY_EXACT_VALUE, reason))
    return violations


def aggregate_violations(explicit_aggregates, query_labels):
    """Report MIN, MAX, SAMPLE and GROUP_CONCAT that return sensitive or raw record values."""
    violations = []
    for aggregate in explicit_aggregates:
        if aggregate.name not in VALUE_RETURNING_AGGREGATES:
            continue
        keyword = AGGREGATE_KEYWORDS[aggregate.name]
        # Aggregate arguments never contain aggregate results of the same scope.
        argument_label = query_labels.of_expression(aggregate.vars, None)
        if argument_label == Label.SENSITIVE:
            names = ", ".join(sensitive_names(aggregate.vars, query_labels))
            reason = f"{keyword} over {names} returns {SENSITIVE_MEANING}."
            violations.append(Violation(Rule.EXPOSES_IDENTIFIER, reason))
        elif argument_label == Label.RECORD_VALUE and aggregate.name in VALUE_LISTING_AGGREGATES:
            names = ", ".join(f"?{name}" for name in sorted(variables_in_expression(aggregate.vars)))
            reason = f"{keyword} over {names} returns raw values of patient records instead of an aggregate."
            violations.append(Violation(Rule.NOT_AGGREGATED, reason))
    return violations


def returned_variable_violations(top_project, group_keys, explicit_aggregates, bind_expressions, query_labels):
    """Report returned variables that are neither GROUP BY keys nor computed from an aggregate."""
    violations = []
    for returned_variable in top_project.PV:
        name = str(returned_variable)
        if name in group_keys:
            continue
        bind_expression = bind_expressions.get(name)
        if bind_expression is not None and uses_explicit_aggregate(bind_expression, explicit_aggregates):
            continue
        reason = f"The query returns ?{name} row by row instead of an aggregate over all patients."
        violations.append(Violation(Rule.NOT_AGGREGATED, reason))
        if query_labels.by_variable.get(name) == Label.SENSITIVE:
            reason = f"The query returns ?{name}, which holds {SENSITIVE_MEANING}."
            violations.append(Violation(Rule.EXPOSES_IDENTIFIER, reason))
    return violations


def exact_date_group_violations(algebra_root):
    """Report an outer GROUP BY on the year, month and day of the same date: one group per exact date.

    The parts are either the date properties of one date node (`sphn:hasYear ?y ; sphn:hasMonth ?m ;
    sphn:hasDay ?d`) or the date functions of one date-time (`YEAR(?a)`, `MONTH(?a)`, `DAY(?a)`),
    written in the GROUP BY or bound to the grouped variables with BIND.
    """
    if algebra_root.name != "SelectQuery":
        return []
    top_project = query_scopes(algebra_root)[0]
    group_keys = []
    bind_expressions = {}
    for node in scope_nodes(top_project):
        if node.name == "Group" and node.expr is not None:
            group_keys.extend(group_expressions(node))
        if node.name == "Extend" and node.var is not None:
            bind_expressions[str(node.var)] = node.expr
    group_names = set()
    # {date-time variable name: {"year", "month", "day"}} grouped through the date functions.
    grouped_parts_by_date_time = {}
    for group_key in group_keys:
        expression = group_key
        if isinstance(group_key, Variable):
            group_names.add(str(group_key))
            expression = bind_expressions.get(str(group_key))
        if isinstance(expression, CompValue) and expression.name in DATE_PART_FUNCTIONS:
            date_time = unwrapped(expression.arg)
            if isinstance(date_time, Variable):
                part = DATE_PART_FUNCTIONS[expression.name]
                grouped_parts_by_date_time.setdefault(str(date_time), set()).add(part)
    grouped_parts_by_date_node = {}
    for subject, predicate, triple_object in patient_patterns(algebra_root):
        final_property = last_property(predicate)
        if final_property in DATE_PART_PROPERTIES and isinstance(triple_object, Variable) \
                and str(triple_object) in group_names:
            grouped_parts_by_date_node.setdefault(subject, set()).add(DATE_PART_PROPERTIES[final_property])
    all_grouped_parts = list(grouped_parts_by_date_node.values()) + list(grouped_parts_by_date_time.values())
    for parts in all_grouped_parts:
        if FULL_DATE <= parts:
            reason = ("The results are grouped by year, month and day of the same date: each group is one exact "
                      "date, almost always a single patient. Group by year or month instead.")
            return [Violation(Rule.GROUPED_BY_EXACT_VALUE, reason)]
    return []


def output_violations(algebra_root, query_labels):
    """Report the outputs of a SELECT query that are not aggregated or that expose identifiers."""
    if algebra_root.name != "SelectQuery":
        return []
    top_project = query_scopes(algebra_root)[0]
    group_keys, explicit_aggregates, bind_expressions = outer_scope_facts(top_project)
    returned_names = set()
    for returned_variable in top_project.PV:
        returned_names.add(str(returned_variable))

    violations = []
    violations.extend(group_key_violations(group_keys, returned_names, query_labels, top_project))
    violations.extend(aggregate_violations(explicit_aggregates, query_labels))
    violations.extend(returned_variable_violations(
        top_project, group_keys, explicit_aggregates, bind_expressions, query_labels,
    ))
    return violations
