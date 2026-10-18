"""Explain what Gate B does with one query: the label of every variable, every check, the verdict.

    python -m pppp.checker.GateB.explain query.rq
    python -m pppp.checker.GateB.explain < query.rq
    python -m pppp.checker.GateB.explain query.rq --only-violations
    python -m pppp.checker.GateB.explain query.rq --rules base    # only the rules of Query_Compliance.md

Most checks decide from the labels of the variables (see variable_labels), so the first part
shows each variable's label and the pattern that gave it; the second part runs the checks of
checker.CHECKS in order and shows what each one found.
"""

import argparse
import sys

from rdflib import Literal, URIRef, Variable
from rdflib.paths import AlternativePath, InvPath, MulPath, NegatedPath, SequencePath
from rdflib.plugins.sparql.parserutils import CompValue

from pppp.checker.GateB.algebra_walk import all_nodes, query_scopes, scope_nodes
from pppp.checker.GateB.checker import run_checks
from pppp.checker.GateB.variable_labels import (
    Label,
    code_node_variables,
    constant_tested_variables,
    is_aggregate_result,
    is_terminology_graph,
    object_label,
    restricted_code_nodes,
    subject_classes_by_variable,
    triple_patterns_with_graph,
)

PREFIXES = {
    "https://biomedit.ch/rdf/sphn-schema/sphn#": "sphn:",
    "https://biomedit.ch/rdf/sphn-schema/sphn/individual#": "sphn-individual:",
    "https://biomedit.ch/rdf/sphn-resource/icd-10-gm/": "icd-10-gm:",
    "https://biomedit.ch/rdf/sphn-resource/ordo/": "ordo:",
    "https://biomedit.ch/rdf/sphn-resource/atc/": "atc:",
    "https://biomedit.ch/rdf/sphn-resource/loinc/": "loinc:",
    "https://biomedit.ch/rdf/sphn-resource/chop/": "chop:",
    "https://biomedit.ch/rdf/sphn-resource/ucum/": "ucum:",
    "http://snomed.info/id/": "snomed:",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#": "rdf:",
    "http://www.w3.org/2000/01/rdf-schema#": "rdfs:",
    "http://www.w3.org/2001/XMLSchema#": "xsd:",
    "http://example.org/graph/": "graph:",
}
LABEL_MEANINGS = {
    Label.CATEGORY: "shared by many patients (a class, code, value-set individual, constant)",
    Label.AGGREGATE: "a COUNT, SUM or AVG",
    Label.BUCKET: "a range of record values (YEAR, FLOOR, a year or month of a date)",
    Label.RECORD_VALUE: "a raw value of one record (date-time, measurement, free text)",
    Label.SENSITIVE: "a patient graph, record IRI or identifier",
}
OPERATORS = {"ConditionalAndExpression": "&&", "ConditionalOrExpression": "||"}
AGGREGATE_KEYWORDS = {"Aggregate_Count": "COUNT", "Aggregate_Sum": "SUM", "Aggregate_Avg": "AVG",
                      "Aggregate_Min": "MIN", "Aggregate_Max": "MAX", "Aggregate_Sample": "SAMPLE",
                      "Aggregate_GroupConcat": "GROUP_CONCAT"}
ARGUMENT_FIELDS = ("arg", "arg1", "arg2", "arg3", "text", "pattern", "flags")


def term_text(term):
    """Return a term as it would be written in a query, with the usual prefixes."""
    if isinstance(term, Variable):
        return f"?{term}"
    if isinstance(term, URIRef):
        text = str(term)
        for namespace, prefix in PREFIXES.items():
            if text.startswith(namespace):
                return prefix + text[len(namespace):]
        return f"<{text}>"
    if isinstance(term, Literal):
        if term.datatype is not None:
            return f'"{term}"^^{term_text(term.datatype)}'
        return f'"{term}"'
    if isinstance(term, SequencePath):
        return "/".join(term_text(step) for step in term.args)
    if isinstance(term, AlternativePath):
        return "|".join(term_text(step) for step in term.args)
    if isinstance(term, InvPath):
        return "^" + term_text(term.arg)
    if isinstance(term, MulPath):
        return term_text(term.path) + term.mod
    if isinstance(term, NegatedPath):
        return "!(" + "|".join(term_text(step) for step in term.args) + ")"
    return str(term)


def expression_text(expression, aggregates=None):
    """Return an algebra expression as compact SPARQL text; `aggregates` maps __agg_n__ to its aggregate."""
    aggregates = aggregates or {}
    if isinstance(expression, Variable) and is_aggregate_result(expression) and str(expression) in aggregates:
        return expression_text(aggregates[str(expression)], aggregates)
    if isinstance(expression, list):
        return ", ".join(expression_text(item, aggregates) for item in expression)
    if not isinstance(expression, CompValue):
        return term_text(expression)
    name = expression.name
    if name in AGGREGATE_KEYWORDS:
        distinct = "DISTINCT " if expression.get("distinct") else ""
        argument = "*" if expression.get("vars") == "*" else expression_text(expression.get("vars"), aggregates)
        return f"{AGGREGATE_KEYWORDS[name]}({distinct}{argument})"
    if name == "RelationalExpression":
        other = expression.other
        other_text = f"({expression_text(other, aggregates)})" if isinstance(other, list) else expression_text(other, aggregates)
        return f"{expression_text(expression.expr, aggregates)} {expression.op} {other_text}"
    if name in OPERATORS:
        parts = [expression.expr] + list(expression.other)
        return f" {OPERATORS[name]} ".join(expression_text(part, aggregates) for part in parts)
    if name in ("AdditiveExpression", "MultiplicativeExpression"):
        text = expression_text(expression.expr, aggregates)
        for operator, operand in zip(expression.op, expression.other):
            text += f" {operator} {expression_text(operand, aggregates)}"
        return f"({text})"
    if name == "UnaryNot":
        return "!" + expression_text(expression.expr, aggregates)
    if name == "Function":
        return f"{term_text(expression.iri)}({expression_text(expression.expr, aggregates)})"
    if name == "OrderCondition":
        return f"{expression.order or ''}({expression_text(expression.expr, aggregates)})"
    if name.startswith("Builtin_"):
        arguments = [expression_text(expression[field], aggregates) for field in ARGUMENT_FIELDS
                     if field in expression and expression[field] is not None]
        return f"{name[len('Builtin_'):]}({', '.join(arguments)})"
    return name


def triple_text(triple):
    return " ".join(term_text(term) for term in triple)


def label_sources(algebra_root, vocabulary, labels):
    """Return {variable name: [(Label, why)]}: every place the query gives the variable a label."""
    sources = {}

    def add(name, label, why):
        sources.setdefault(str(name), []).append((label, why))

    patterns = triple_patterns_with_graph(algebra_root)
    code_nodes = code_node_variables(patterns)
    shared_code_nodes = restricted_code_nodes(patterns, code_nodes, constant_tested_variables(algebra_root))
    classes_by_variable = subject_classes_by_variable(patterns)
    for graph_term, triple in patterns:
        subject, predicate, triple_object = triple
        where = f"in `{triple_text(triple)}`"
        if is_terminology_graph(graph_term):
            for term in triple:
                if isinstance(term, Variable):
                    add(term, Label.CATEGORY, f"{where}, terminology graph")
            continue
        if isinstance(subject, Variable):
            add(subject, Label.SENSITIVE, f"subject {where}: a record node")
        if isinstance(predicate, Variable):
            add(predicate, Label.CATEGORY, f"predicate {where}: a schema property")
        if isinstance(triple_object, Variable):
            subject_classes = classes_by_variable.get(str(subject), frozenset())
            label = object_label(subject, predicate, vocabulary, code_nodes, subject_classes, shared_code_nodes)
            why = f"object {where}"
            if label == Label.SENSITIVE:
                why += ": points at a record (or an identifier)"
            if isinstance(subject, Variable) and str(subject) in code_nodes and label != Label.SENSITIVE:
                restricted = str(subject) in shared_code_nodes
                why += (", a code node with a restricted coding system: a shared code" if restricted else
                        ", a code node whose coding system is not restricted: may be a per-patient value")
            add(triple_object, label, why)
    for node in all_nodes(algebra_root):
        if node.name == "Graph" and isinstance(node.term, Variable):
            add(node.term, Label.SENSITIVE, f"GRAPH ?{node.term}: a patient graph")
        if node.name == "values":
            for row in node.res:
                for variable in row:
                    add(variable, Label.CATEGORY, "set by VALUES to a constant")
    for scope in query_scopes(algebra_root):
        aggregates = {}
        nodes = scope_nodes(scope)
        for node in nodes:
            if node.name == "AggregateJoin":
                for aggregate in node.A:
                    aggregates[str(aggregate.res)] = aggregate
        for node in nodes:
            if node.name == "Extend" and node.var is not None and not is_aggregate_result(node.var):
                implicit = aggregates.get(str(node.expr))
                if implicit is not None and implicit.name == "Aggregate_Sample" and str(implicit.vars) == str(node.var):
                    continue  # the SAMPLE rdflib adds to return a GROUP BY key
                label = labels.of_expression(node.expr, scope)
                add(node.var, label, f"computed as {expression_text(node.expr, aggregates)}")
    return sources


def explain(query_text, only_violations=False, rules="extended"):
    """Return the explanation of a query as text; `rules` is "extended" (default) or "base"."""
    run = run_checks(query_text, rules)
    lines = [f"RULES  {run.rules.value}"]
    if run.context is not None and not only_violations:
        labels = run.context.labels
        sources = label_sources(run.context.algebra_root, run.context.vocabulary, labels)
        lines.append("VARIABLES (label = the most identifying one the query gives it)")
        for name in sorted(labels.by_variable, key=lambda n: (-labels.by_variable[n], n)):
            label = labels.by_variable[name]
            lines.append(f"  ?{name:<22} {label.name:<13} {LABEL_MEANINGS[label]}")
            deciding = [why for source_label, why in sources.get(name, []) if source_label == label]
            for why in list(dict.fromkeys(deciding))[:3]:
                lines.append(f"      because: {why}")
        lines.append("")
    lines.append("CHECKS")
    if run.extension_violations:
        for violation in run.extension_violations:
            lines.append(f"  VIOLATION  Virtuoso extensions: {violation.rule.value}: {violation.reason}")
    elif not only_violations:
        lines.append("  ok         Virtuoso extensions  (looks for DEFINE and OPTION clauses outside the allow list)")
    if run.parse_violation is not None:
        lines.append(f"  VIOLATION  parsing: {run.parse_violation.rule.value}: {run.parse_violation.reason}")
        lines.append("             (the other checks need a parsed SELECT/ASK/... query and did not run)")
    current_layer = None
    for check, violations in run.results:
        if only_violations and not violations:
            continue
        if check.layer != current_layer:
            current_layer = check.layer
            lines.append(f"  layer {check.layer}")
        if not violations:
            lines.append(f"    ok         {check.name}  (looks for {check.looks_for})")
        for violation in violations:
            lines.append(f"    VIOLATION  {check.name}: {violation.rule.value}: {violation.reason}")
    if run.skipped and not only_violations:
        names = ", ".join(check.name for check in run.skipped)
        lines.append(f"  skipped    {names}  (needed only for the extended rules)")
    lines.append("")
    all_violations = run.violations()
    if all_violations:
        rules = ", ".join(dict.fromkeys(violation.rule.value for violation in all_violations))
        lines.append(f"VERDICT  non-compliant ({rules})")
    else:
        lines.append("VERDICT  no violation found (compliant for Gate B)")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explain what Gate B does with one SPARQL query.")
    parser.add_argument("query_file", nargs="?", help="a .rq file (default: read the query from stdin)")
    parser.add_argument("--only-violations", action="store_true", help="show only the checks that found something")
    parser.add_argument("--rules", choices=["extended", "base"], default="extended",
                        help="extended (default): every rule; base: only the rules of Query_Compliance.md")
    arguments = parser.parse_args(argv)
    if arguments.query_file:
        with open(arguments.query_file, encoding="utf-8") as query_file:
            query_text = query_file.read()
    else:
        query_text = sys.stdin.read()
    print(explain(query_text, only_violations=arguments.only_violations, rules=arguments.rules))


if __name__ == "__main__":
    main()
