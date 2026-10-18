"""Labels of the variables of a query: what kind of value each one can hold.

Every variable gets one label, from the least to the most identifying:
- CATEGORY: a terminology class, a value-set individual, a constant, a terminology label,
  or a code value (see "code node" below);
- AGGREGATE: the result of COUNT, SUM or AVG;
- BUCKET: a grouping function (YEAR, FLOOR, ROUND...) of a record value, or the year, month or
  day of a date (sphn:hasYear...), each shared by many patients. FLOOR, CEIL and ROUND of a value
  scaled up first (`ROUND(?v * 1000)`) keep its decimals and stay RECORD_VALUE;
- RECORD_VALUE: a literal of a patient record (a date-time, a measurement, a free text);
- SENSITIVE: a patient graph IRI, a record IRI, or an identifier literal.

Labels start from the triple patterns and spread through BIND, VALUES, aggregates and
subquery projections. A computed value gets the highest label of the values it is computed
from, except in expressions that only return true or false (EXISTS, BOUND, comparisons,
string tests) and in the condition of IF. A function these rules do not know passes the
highest label on.

Whether an object property points at value-set individuals or at records is decided for the
class of its subject when the query fixes it (`?h a sphn:Hash ; sphn:hasAlgorithm ?alg`), from the
SHACL shapes, and for the property alone otherwise.

A code node is the object of `sphn:hasCode`. Its literals (`sphn:hasIdentifier "E11.90"`,
`sphn:hasName "Type 2 diabetes mellitus"`) are the code of a diagnosis or of a drug article,
shared by many patients, when the query restricts the node's coding system
(`FILTER (STRSTARTS(?coding_system, "ICD-10-GM"))`): they are then categories. Without that
restriction they are record values: in the mock data, 230,000 of the 238,000 sphn:Code nodes
carry a per-patient identifier ("ID-10000540000003") and a free-text name, and only the nodes
whose coding system is ICD-10-GM hold shared codes. Grouping by them or returning them is then
row-level output, while filtering them with a constant (`STRSTARTS(?icd, "E11")`) is fine.

A variable keeps the highest label it gets anywhere in the query, subqueries included.
"""

import enum

from rdflib import RDF, Literal, URIRef, Variable
from rdflib.paths import AlternativePath, MulPath, SequencePath
from rdflib.plugins.sparql.parserutils import CompValue

from pppp.checker.GateB.algebra_walk import all_nodes, child_nodes, query_scopes, scope_nodes
from pppp.checker.GateB.data_layout import TERMINOLOGY_GRAPH

SPHN_HAS_CODE = URIRef("https://biomedit.ch/rdf/sphn-schema/sphn#hasCode")
SPHN_HAS_CODING_SYSTEM = URIRef("https://biomedit.ch/rdf/sphn-schema/sphn#hasCodingSystemAndVersion")
# Tests that restrict a value to a constant: `?x = "..."`, STRSTARTS(?x, "..."), REGEX(?x, "...").
RESTRICTING_TESTS = {
    "Builtin_STRSTARTS": ("arg1", "arg2"),
    "Builtin_STRENDS": ("arg1", "arg2"),
    "Builtin_CONTAINS": ("arg1", "arg2"),
    "Builtin_REGEX": ("text", "pattern"),
}
# Expressions whose result is only true or false: they never pass a label on.
BOOLEAN_EXPRESSIONS = {
    "RelationalExpression", "ConditionalAndExpression", "ConditionalOrExpression", "UnaryNot",
    "Builtin_EXISTS", "Builtin_NOTEXISTS", "Builtin_BOUND", "Builtin_sameTerm",
    "Builtin_isIRI", "Builtin_isURI", "Builtin_isBLANK", "Builtin_isLITERAL", "Builtin_isNUMERIC",
    "Builtin_LANGMATCHES", "Builtin_REGEX", "Builtin_CONTAINS", "Builtin_STRSTARTS", "Builtin_STRENDS",
}
# Functions that map many record values to one shared value.
BUCKETING_FUNCTIONS = {
    "Builtin_YEAR", "Builtin_MONTH", "Builtin_DAY", "Builtin_HOURS",
    "Builtin_FLOOR", "Builtin_CEIL", "Builtin_ROUND",
}
COUNTING_AGGREGATES = {"Aggregate_Count", "Aggregate_Sum", "Aggregate_Avg"}
# Bucketing functions that round a number: a bucket only if they do not scale the value up first.
ROUNDING_FUNCTIONS = {"Builtin_FLOOR", "Builtin_CEIL", "Builtin_ROUND"}
# Parts of a date (sphn:BirthDate, sphn:DeathDate...): a year or a month is a bucket of dates.
# All three of the same date fixed or grouped together are an exact date (Rule.EXACT_DATE,
# Rule.GROUPED_BY_EXACT_VALUE).
DATE_PART_PROPERTIES = {
    URIRef("https://biomedit.ch/rdf/sphn-schema/sphn#hasYear"),
    URIRef("https://biomedit.ch/rdf/sphn-schema/sphn#hasMonth"),
    URIRef("https://biomedit.ch/rdf/sphn-schema/sphn#hasDay"),
}


class Label(enum.IntEnum):
    """Kind of value a variable can hold; a higher value is more identifying."""

    CATEGORY = 1
    AGGREGATE = 2
    BUCKET = 3
    RECORD_VALUE = 4
    SENSITIVE = 5


def triple_patterns_with_graph(node, graph_term=None):
    """Return every triple pattern below `node` as a list of (graph_term, triple).

    `graph_term` is the term of the closest enclosing GRAPH clause: a Variable, a URIRef, or
    None outside any GRAPH clause (Virtuoso's default graph, the union of all graphs).
    """
    patterns = []
    if node.name == "BGP":
        for triple in node.triples:
            patterns.append((graph_term, triple))
    if node.name == "Graph":
        graph_term = node.term
    for child in child_nodes(node):
        patterns.extend(triple_patterns_with_graph(child, graph_term))
    return patterns


def is_terminology_graph(graph_term):
    """Return True for the graph that holds the terminologies (no patient data)."""
    # rdflib terms never compare equal to plain strings, so compare the IRI text.
    return isinstance(graph_term, URIRef) and str(graph_term) == TERMINOLOGY_GRAPH


def last_steps(predicate):
    """Return the properties a predicate can end with, as a list of (property, previous_property).

    For `p1/p2` the result is [(p2, p1)]; for `p1|p2` it is [(p1, None), (p2, None)]; a plain
    property `p` gives [(p, None)]. A step that is not a plain property (variable, inverse or
    negated path) is returned as (None, None).
    """
    if isinstance(predicate, URIRef):
        return [(predicate, None)]
    if isinstance(predicate, SequencePath):
        steps = last_steps(predicate.args[-1])
        if len(predicate.args) < 2 or not isinstance(predicate.args[-2], URIRef):
            return steps
        previous_property = predicate.args[-2]
        steps_with_previous = []
        for last_property, ignored_previous in steps:
            steps_with_previous.append((last_property, previous_property))
        return steps_with_previous
    if isinstance(predicate, AlternativePath):
        steps = []
        for alternative in predicate.args:
            steps.extend(last_steps(alternative))
        return steps
    if isinstance(predicate, MulPath):
        return last_steps(predicate.path)
    return [(None, None)]


def code_node_variables(patterns):
    """Return the names of the variables that are the object of sphn:hasCode."""
    names = set()
    for graph_term, (subject, predicate, triple_object) in patterns:
        if not isinstance(triple_object, Variable):
            continue
        for last_property, previous_property in last_steps(predicate):
            if last_property == SPHN_HAS_CODE:
                names.add(str(triple_object))
    return names


def subject_classes_by_variable(patterns):
    """Return {variable name: set of class IRIs} from the patterns `?subject a <class>`."""
    classes = {}
    for graph_term, (subject, predicate, triple_object) in patterns:
        if predicate == RDF.type and isinstance(subject, Variable) and isinstance(triple_object, URIRef):
            classes.setdefault(str(subject), set()).add(triple_object)
    return classes


def constant_tested_variables(algebra_root):
    """Return the names of the variables a filter restricts to a constant (=, IN, STRSTARTS, REGEX...)."""
    names = set()
    for node in all_nodes(algebra_root):
        tested_pairs = []
        if node.name == "RelationalExpression" and str(node.op) in ("=", "IN"):
            tested_pairs = [(node.expr, node.other), (node.other, node.expr)]
        elif node.name in RESTRICTING_TESTS:
            tested_field, constant_field = RESTRICTING_TESTS[node.name]
            tested_pairs = [(node[tested_field], node[constant_field])]
        for tested, constant in tested_pairs:
            while isinstance(tested, CompValue) and tested.name == "Builtin_STR":
                tested = tested.arg
            constants = constant if isinstance(constant, list) else [constant]
            if isinstance(tested, Variable) and constants and all(isinstance(c, Literal) for c in constants):
                names.add(str(tested))
    return names


def restricted_code_nodes(patterns, code_nodes, tested_names):
    """Return the code nodes whose coding system the query fixes or restricts to a constant."""
    restricted = set()
    for graph_term, (subject, predicate, triple_object) in patterns:
        if predicate != SPHN_HAS_CODING_SYSTEM or not isinstance(subject, Variable) or str(subject) not in code_nodes:
            continue
        if isinstance(triple_object, Literal) or (isinstance(triple_object, Variable) and str(triple_object) in tested_names):
            restricted.add(str(subject))
    return restricted


def object_label(subject, predicate, vocabulary, code_nodes, subject_classes=frozenset(), shared_code_nodes=frozenset()):
    """Return the Label of the object of a triple pattern inside a patient graph.

    `subject_classes` are the classes the query gives the subject; they only apply to a plain
    property (or alternatives of plain properties), whose last step starts at the subject.
    `shared_code_nodes` are the code nodes whose coding system the query restricts: only their
    literals are shared codes (see the module docstring).
    """
    if not isinstance(predicate, (URIRef, AlternativePath)):
        subject_classes = frozenset()
    highest_label = Label.CATEGORY
    for last_property, previous_property in last_steps(predicate):
        if last_property is None:
            step_label = Label.SENSITIVE
        elif last_property == RDF.type:
            step_label = Label.CATEGORY
        elif last_property in DATE_PART_PROPERTIES:
            step_label = Label.BUCKET
        elif last_property in vocabulary.datatype_properties:
            if previous_property is not None:
                # `?dx sphn:hasCode/sphn:hasIdentifier ?id`: the code node has no name to restrict.
                subject_is_code_node = previous_property == SPHN_HAS_CODE
                subject_is_shared_code = False
            else:
                subject_is_code_node = isinstance(subject, Variable) and str(subject) in code_nodes
                subject_is_shared_code = isinstance(subject, Variable) and str(subject) in shared_code_nodes
            if subject_is_shared_code:
                step_label = Label.CATEGORY
            elif subject_is_code_node:
                step_label = Label.RECORD_VALUE
            elif last_property in vocabulary.identifier_properties:
                step_label = Label.SENSITIVE
            else:
                step_label = Label.RECORD_VALUE
        elif vocabulary.is_value_set(subject_classes, last_property) is not None:
            if vocabulary.is_value_set(subject_classes, last_property):
                step_label = Label.CATEGORY
            else:
                step_label = Label.SENSITIVE
        elif last_property in vocabulary.value_set_properties:
            step_label = Label.CATEGORY
        else:
            # Object properties and properties the schema does not define point at records.
            step_label = Label.SENSITIVE
        highest_label = max(highest_label, step_label)
    return highest_label


def raise_label(labels_by_name, name, label):
    """Set the label of `name` to `label` unless it already has an equal or higher one.

    Returns True if the label was set.
    """
    if name in labels_by_name and labels_by_name[name] >= label:
        return False
    labels_by_name[name] = label
    return True


def is_aggregate_result(variable):
    """Return True for the variables rdflib creates for aggregate results (`__agg_1__`...)."""
    return str(variable).startswith("__agg_")


def expression_label(expression, labels_by_variable, aggregate_labels):
    """Return the Label of the value an expression computes.

    `aggregate_labels` is {aggregate result name: Label} for the query scope of the expression:
    rdflib reuses the names `__agg_1__`, `__agg_2__`... in every subquery.
    """
    if isinstance(expression, Variable):
        if is_aggregate_result(expression):
            return aggregate_labels.get(str(expression), Label.CATEGORY)
        # A variable no pattern binds (e.g. the "else" branch of IF(cond, ?g, ?unbound)) adds nothing.
        return labels_by_variable.get(str(expression), Label.CATEGORY)
    if isinstance(expression, list):
        highest_label = Label.CATEGORY
        for item in expression:
            item_label = expression_label(item, labels_by_variable, aggregate_labels)
            highest_label = max(highest_label, item_label)
        return highest_label
    if not isinstance(expression, CompValue):
        # Literals, IRIs, operators and keywords such as "*".
        return Label.CATEGORY
    if expression.name in BOOLEAN_EXPRESSIONS:
        return Label.CATEGORY
    if expression.name == "Builtin_IF":
        then_label = expression_label(expression.arg2, labels_by_variable, aggregate_labels)
        else_label = expression_label(expression.arg3, labels_by_variable, aggregate_labels)
        return max(then_label, else_label)

    argument_label = Label.CATEGORY
    for field_name, field_value in expression.items():
        if field_name == "_vars":
            continue
        field_label = expression_label(field_value, labels_by_variable, aggregate_labels)
        argument_label = max(argument_label, field_label)
    if expression.name in BUCKETING_FUNCTIONS and argument_label == Label.RECORD_VALUE:
        if expression.name in ROUNDING_FUNCTIONS and scale_factor(expression.arg) > 1:
            # ROUND(?v * 1000) keeps three decimals: as fine as the value itself, not a bucket.
            return Label.RECORD_VALUE
        return Label.BUCKET
    return argument_label


def scale_factor(expression):
    """Return the constant factor an expression multiplies its value by: 1000 for `?v * 1000`, 0.1 for `?v / 10`.

    Only the outer `*` and `/` by numeric constants count; anything else gives 1.
    """
    if not isinstance(expression, CompValue) or expression.name != "MultiplicativeExpression":
        return 1
    factor = 1.0
    for operator, operand in zip(expression.op, expression.other):
        if not isinstance(operand, Literal):
            continue
        try:
            value = float(operand.toPython())
        except (TypeError, ValueError):
            continue
        if value == 0:
            continue
        factor = factor * value if operator == "*" else factor / value
    return factor


def aggregate_label(aggregate, labels_by_variable):
    """Return the Label of the result of an aggregate (COUNT, MIN, SAMPLE...)."""
    if aggregate.name in COUNTING_AGGREGATES:
        return Label.AGGREGATE
    # MIN, MAX, SAMPLE and GROUP_CONCAT return values of their argument, which never
    # contains an aggregate result of the same scope.
    return expression_label(aggregate.vars, labels_by_variable, {})


class QueryLabels:
    """Labels of the variables of one query.

    by_variable: {variable name: Label} for the variables written in the query.
    aggregates_by_scope: {id of a Project node: {aggregate result name: Label}}.
    """

    def __init__(self, by_variable, aggregates_by_scope):
        self.by_variable = by_variable
        self.aggregates_by_scope = aggregates_by_scope

    def of_expression(self, expression, scope):
        """Return the Label of an expression that belongs to the query scope `scope` (a Project node)."""
        aggregate_labels = self.aggregates_by_scope.get(id(scope), {})
        return expression_label(expression, self.by_variable, aggregate_labels)


def variable_labels(algebra_root, vocabulary):
    """Return the QueryLabels of a query.

    `vocabulary` is a pppp.checker.GateB.vocabulary.Vocabulary.
    """
    labels_by_variable = {}
    patterns = triple_patterns_with_graph(algebra_root)
    code_nodes = code_node_variables(patterns)
    shared_code_nodes = restricted_code_nodes(patterns, code_nodes, constant_tested_variables(algebra_root))
    classes_by_variable = subject_classes_by_variable(patterns)

    for graph_term, (subject, predicate, triple_object) in patterns:
        if is_terminology_graph(graph_term):
            for term in (subject, predicate, triple_object):
                if isinstance(term, Variable):
                    raise_label(labels_by_variable, str(term), Label.CATEGORY)
            continue
        if isinstance(subject, Variable):
            raise_label(labels_by_variable, str(subject), Label.SENSITIVE)
        if isinstance(predicate, Variable):
            # A predicate can only hold a property of the schema, shared by all patients.
            raise_label(labels_by_variable, str(predicate), Label.CATEGORY)
        if isinstance(triple_object, Variable):
            subject_classes = classes_by_variable.get(str(subject), frozenset())
            label = object_label(subject, predicate, vocabulary, code_nodes, subject_classes, shared_code_nodes)
            raise_label(labels_by_variable, str(triple_object), label)

    for node in all_nodes(algebra_root):
        if node.name == "Graph" and isinstance(node.term, Variable):
            raise_label(labels_by_variable, str(node.term), Label.SENSITIVE)
        if node.name == "values":
            for row in node.res:
                for variable in row:
                    raise_label(labels_by_variable, str(variable), Label.CATEGORY)

    # Computed variables, per query scope: (scope, variable name, BIND expression) and
    # (scope, aggregate result name, aggregate node).
    bind_definitions = []
    aggregate_definitions = []
    aggregates_by_scope = {}
    for scope in query_scopes(algebra_root):
        aggregates_by_scope[id(scope)] = {}
        for node in scope_nodes(scope):
            if node.name == "Extend":
                bind_definitions.append((scope, str(node.var), node.expr))
            if node.name == "AggregateJoin":
                for aggregate in node.A:
                    aggregate_definitions.append((scope, str(aggregate.res), aggregate))
    query_labels = QueryLabels(labels_by_variable, aggregates_by_scope)

    # A definition can use a variable defined further on: repeat until no label changes.
    # Labels only go up and there are five of them, so the loop ends.
    labels_changed = True
    while labels_changed:
        labels_changed = False
        for scope, aggregate_name, aggregate in aggregate_definitions:
            label = aggregate_label(aggregate, labels_by_variable)
            if raise_label(aggregates_by_scope[id(scope)], aggregate_name, label):
                labels_changed = True
        for scope, variable_name, expression in bind_definitions:
            label = query_labels.of_expression(expression, scope)
            if raise_label(labels_by_variable, variable_name, label):
                labels_changed = True
    return query_labels
