"""Layer 3 of the structural checks: patients selected through sensitive values.

Uses the variable labels of pppp.checker.GateB.variable_labels. A query selects specific
patients when it compares a SENSITIVE value (a patient graph, a record IRI, an identifier,
or anything computed from them) with a constant:
- in a comparison: `FILTER (STR(?g) = "http://example.org/graph/...")`;
- in a string test: `FILTER (CONTAINS(STR(?g), "0042"))`, `REGEX(?pseudo_id, "^ID-1021025")`;
- in a triple pattern: `?subject sphn:hasIdentifier "ID-10210250000022"`.
It also selects specific patients when a subquery returns SENSITIVE values cut by LIMIT:
`{ SELECT ?g WHERE { ... } ORDER BY STR(?g) LIMIT 1 }` passes one patient graph on.
Excluding a patient (`FILTER (?g != <...>)`, `NOT IN`) is reported as EXCLUDES_PATIENT: compared
with the full count, the result reveals that patient.

An aggregated query without identifiers can still single out patients (RULES.md):
- SMALL_GROUP: a HAVING that keeps only groups of at most SMALL_GROUP_SIZE patients
  (`HAVING (COUNT(DISTINCT ?g) = 1)`, "codes that only one patient has"). Also written as
  `IN (1, 2)`, `!(COUNT(...) > 5)`, on the count's alias (`HAVING (?n = 1)`), inside a
  subquery, or as an outer FILTER on a count a subquery returns;
- EXACT_DATE: an exact timestamp (`sphn:hasAdmissionDateTime "2024-02-12T14:06:11"^^xsd:dateTime`,
  `FILTER (?a = ...)`, `STR(?a) = "..."`, `IN`, VALUES, a range whose two ends are equal), or a
  full date: year, month and day of the same date fixed (`sphn:hasYear 1975 ; sphn:hasMonth 3 ;
  sphn:hasDay 14`, `YEAR(?a) = 2024 && MONTH(?a) = 2 && DAY(?a) = 12`, `STRSTARTS(STR(?a), "2024-02-12")`).
  Years, months and ranges of dates are fine;
- QUASI_IDENTIFIERS: QUASI_IDENTIFIER_LIMIT or more identifying attributes of the patient (sex,
  birth, nationality, residence, civil status, death, age) fixed to exact values ("Swiss men
  born in 1975"). Ranges and GROUP BY fix nothing.
"""

import re

from rdflib import RDF, Literal, URIRef, Variable
from rdflib.paths import AlternativePath, InvPath, MulPath, SequencePath
from rdflib.plugins.sparql.parserutils import CompValue

from pppp.checker.GateB.algebra_walk import (
    all_nodes,
    constant_bound_variables,
    query_scopes,
    scope_nodes,
    variables_in_expression,
)
from pppp.checker.GateB.data_layout import PATIENT_GRAPH_PREFIX
from pppp.checker.GateB.pattern_checks import is_patient_record_iri
from pppp.checker.GateB.rules import Rule, Violation
from pppp.checker.GateB.variable_labels import (
    Label,
    code_node_variables,
    constant_tested_variables,
    is_terminology_graph,
    last_steps,
    restricted_code_nodes,
    triple_patterns_with_graph,
)

# String tests and their (tested value field, constant field).
STRING_TEST_FIELDS = {
    "Builtin_CONTAINS": ("arg1", "arg2"),
    "Builtin_STRSTARTS": ("arg1", "arg2"),
    "Builtin_STRENDS": ("arg1", "arg2"),
    "Builtin_sameTerm": ("arg1", "arg2"),
    "Builtin_REGEX": ("text", "pattern"),
}
# Operators rdflib puts between a LIMIT and the SELECT it cuts (SELECT DISTINCT, SELECT REDUCED).
MODIFIERS_ABOVE_PROJECT = {"Distinct", "Reduced"}
# Comparisons that keep every patient but the ones compared with.
EXCLUDING_OPERATORS = {"!=", "NOT IN"}
SPHN_HAS_CODE = URIRef("https://biomedit.ch/rdf/sphn-schema/sphn#hasCode")


def compared_sides(node):
    """Return (side, other_side) pairs compared by a comparison or string-test node, or []."""
    if node.name == "RelationalExpression":
        return [(node.expr, node.other), (node.other, node.expr)]
    if node.name in STRING_TEST_FIELDS:
        tested_field, constant_field = STRING_TEST_FIELDS[node.name]
        return [(node[tested_field], node[constant_field])]
    return []


def is_patient_iri(iri):
    """Return True for the IRI of a patient graph or of a patient record."""
    return str(iri).startswith(PATIENT_GRAPH_PREFIX) or is_patient_record_iri(iri)


def keeps_every_patient_graph(node, constant):
    """Return True for a prefix test that every patient graph passes.

    `STRSTARTS(STR(?g), "http://example.org/graph/")` and `REGEX(STR(?g), "^http://example.org/graph/")`
    test the namespace of the patient graphs, not a patient.
    """
    if not isinstance(constant, Literal):
        return False
    text = str(constant)
    if node.name == "Builtin_STRSTARTS":
        return PATIENT_GRAPH_PREFIX.startswith(text)
    if node.name == "Builtin_REGEX" and text.startswith("^"):
        prefix = text[1:]
        has_special_characters = any(character in prefix for character in "$|()[]{}*+?\\")
        # An unescaped "." matches any character, so it still matches every patient graph.
        return not has_special_characters and PATIENT_GRAPH_PREFIX.startswith(prefix)
    return False


def sensitive_comparison_violations(algebra_root, query_labels):
    """Report comparisons of a SENSITIVE value with a constant, once per set of variables.

    A variable that VALUES or BIND only sets to constants counts as a constant.
    """
    violations = []
    reported_variable_sets = []
    constant_names = constant_bound_variables(algebra_root)
    for scope in query_scopes(algebra_root):
        for node in scope_nodes(scope):
            for side, other_side in compared_sides(node):
                if variables_in_expression(other_side) - constant_names:
                    continue
                if keeps_every_patient_graph(node, other_side):
                    continue
                if isinstance(other_side, URIRef) and not is_patient_iri(other_side):
                    # A record compared with a terminology class (`?code = snomed:304253006`) matches no
                    # record: it selects nobody. Patient and record IRIs are reported by layer 2.
                    continue
                if query_labels.of_expression(side, scope) != Label.SENSITIVE:
                    continue
                sensitive_names = []
                for name in sorted(variables_in_expression(side)):
                    if query_labels.by_variable.get(name) == Label.SENSITIVE:
                        sensitive_names.append(f"?{name}")
                if sensitive_names in reported_variable_sets:
                    continue
                reported_variable_sets.append(sensitive_names)
                if node.name == "RelationalExpression" and str(node.op) in EXCLUDING_OPERATORS:
                    reason = (f"A filter excludes specific values of {', '.join(sensitive_names)} (a patient graph, "
                              "record or identifier): compared with the full count, the result reveals that patient.")
                    violations.append(Violation(Rule.EXCLUDES_PATIENT, reason))
                    continue
                reason = (f"A filter compares a value computed from {', '.join(sensitive_names)} "
                          "(a patient graph, record or identifier) with a constant: it selects specific patients.")
                violations.append(Violation(Rule.SINGLE_PATIENT, reason))
    return violations


def code_node_names(patterns):
    """Return the names of the code nodes (objects of sphn:hasCode) that hold nothing else.

    A variable that is also the object of another property (`?dx sphn:hasCode ?c .
    ?dx sphn:hasAdministrativeCase ?c`) may hold a record, so it is not a code node here.
    """
    code_nodes = code_node_variables(patterns)
    for graph_term, (subject, predicate, triple_object) in patterns:
        if not isinstance(triple_object, Variable) or predicate == RDF.type:
            continue
        reaches_code_node = False
        for last_property, previous_property in last_steps(predicate):
            if last_property == SPHN_HAS_CODE:
                reaches_code_node = True
        if not reaches_code_node:
            code_nodes.discard(str(triple_object))
    return code_nodes


def is_record_identifier_text(text):
    """Return True for the per-patient identifiers of the mock code nodes: "ID-10000540000003".

    Also accepts the forms a filter writes them in: "^ID-1000054", "ID-10000540000003$".
    """
    return re.fullmatch(r"\^?ID-\d+\$?", text) is not None


def is_record_identifier_fragment(text):
    """Return True for a part of a per-patient identifier a substring test can look for.

    The identifiers are "ID-" + the 7-digit patient number + a 7-digit sequence, so a run of 7 or
    more digits ("10000540000003", "1000054", "^1000054") points at one patient. Shorter digit
    runs and codes with letters ("E11", "I10.90") do not.
    """
    return is_record_identifier_text(text) or re.fullmatch(r"\^?(ID-)?\d{7,}\$?", text) is not None


def code_identifier_targets(algebra_root, vocabulary):
    """Return (patterns, code nodes, unrestricted code nodes, identifier variables of those code nodes).

    A code node whose coding system the query does not restrict may hold a per-patient
    identifier instead of a shared code (see variable_labels).
    """
    patterns = triple_patterns_with_graph(algebra_root)
    code_nodes = code_node_names(patterns)
    shared_code_nodes = restricted_code_nodes(patterns, code_nodes, constant_tested_variables(algebra_root))
    unrestricted_code_nodes = code_nodes - shared_code_nodes
    identifier_names = set()
    for graph_term, (subject, predicate, triple_object) in patterns:
        if not isinstance(triple_object, Variable):
            continue
        for last_property, previous_property in last_steps(predicate):
            if last_property not in vocabulary.identifier_properties:
                continue
            on_code_node = isinstance(subject, Variable) and str(subject) in unrestricted_code_nodes
            if on_code_node or previous_property == SPHN_HAS_CODE:
                identifier_names.add(str(triple_object))
    return patterns, code_nodes, unrestricted_code_nodes, identifier_names


def identifier_literal_violations(algebra_root, vocabulary):
    """Report triple patterns that fix an identifier property to a literal.

    The identifier of a code node is a code shared by many patients ("E11.90"), so fixing it
    selects a group, unless the code node's coding system is not restricted and the literal is
    a per-patient identifier ("ID-10000540000003"): then it selects one patient.
    """
    violations = []
    patterns, code_nodes, unrestricted_code_nodes, identifier_names = code_identifier_targets(algebra_root, vocabulary)
    for graph_term, (subject, predicate, triple_object) in patterns:
        if is_terminology_graph(graph_term) or not isinstance(triple_object, Literal):
            continue
        for last_property, previous_property in last_steps(predicate):
            if last_property not in vocabulary.identifier_properties:
                continue
            on_code_node = isinstance(subject, Variable) and str(subject) in code_nodes
            if on_code_node or previous_property == SPHN_HAS_CODE:
                unrestricted = previous_property == SPHN_HAS_CODE or str(subject) in unrestricted_code_nodes
                if not (unrestricted and is_record_identifier_text(str(triple_object))):
                    continue
                reason = (f"Triple pattern fixes the code identifier {predicate.n3()} to {triple_object.n3()}, a "
                          "per-patient identifier (the code node's coding system is not restricted): it selects one patient.")
            else:
                reason = f"Triple pattern fixes {predicate.n3()} to {triple_object.n3()}: it selects one patient or record."
            violations.append(Violation(Rule.SINGLE_PATIENT, reason))
    return violations


def code_identifier_comparison_violations(algebra_root, vocabulary):
    """Report filters and VALUES that compare a code identifier with a per-patient identifier.

    `FILTER (?id = "ID-10000540000003")` on a code node whose coding system is not restricted
    selects one patient; `FILTER (?id != "ID-...")` excludes one. A substring test also selects
    one with a part of the identifier: `CONTAINS(?id, "10000540000003")`.
    """
    patterns, code_nodes, unrestricted_code_nodes, identifier_names = code_identifier_targets(algebra_root, vocabulary)
    if not identifier_names:
        return []
    violations = []
    reported = set()
    for node in all_nodes(algebra_root):
        tested_pairs = []
        if node.name == "values":
            for row in node.res:
                for variable, value in row.items():
                    tested_pairs.append((variable, value, "="))
        elif node.name == "RelationalExpression":
            tested_pairs = [(node.expr, node.other, str(node.op)), (node.other, node.expr, str(node.op))]
        elif node.name in STRING_TEST_FIELDS:
            tested_field, constant_field = STRING_TEST_FIELDS[node.name]
            operator = "=" if node.name == "Builtin_sameTerm" else "substring"
            tested_pairs = [(node[tested_field], node[constant_field], operator)]
        for tested, constant, operator in tested_pairs:
            while isinstance(tested, CompValue) and tested.name == "Builtin_STR":
                tested = tested.arg
            if not isinstance(tested, Variable) or str(tested) not in identifier_names:
                continue
            constants = constant if isinstance(constant, list) else [constant]
            matches = is_record_identifier_fragment if operator == "substring" else is_record_identifier_text
            if not any(isinstance(c, Literal) and matches(str(c)) for c in constants):
                continue
            rule = Rule.EXCLUDES_PATIENT if operator in EXCLUDING_OPERATORS else Rule.SINGLE_PATIENT
            if (str(tested), rule) in reported:
                continue
            reported.add((str(tested), rule))
            what = "excludes" if rule == Rule.EXCLUDES_PATIENT else "selects"
            reason = (f"A filter compares the code identifier ?{tested} with a per-patient identifier "
                      f"(the code node's coding system is not restricted): it {what} one patient.")
            violations.append(Violation(rule, reason))
    return violations


def limited_subquery_violations(algebra_root, query_labels):
    """Report subqueries cut by LIMIT that pick specific patients or records, once per subquery.

    A cut subquery picks specific patients when it:
    - returns a SENSITIVE value (`{ SELECT ?g ... LIMIT 1 }`);
    - is ordered by a SENSITIVE value (`{ SELECT ?a ... ORDER BY STR(?g) LIMIT 1 }`): the first
      patients by graph IRI, whatever it returns;
    - returns raw record values row by row (`{ SELECT ?v ... ORDER BY ?v OFFSET 41 LIMIT 1 }`):
      LIMIT and OFFSET then pick individual records by position. A subquery that aggregates
      (`GROUP BY ?code ... LIMIT 10`) picks groups, which is fine.
    """
    violations = []
    top_project = query_scopes(algebra_root)[0]
    for node in all_nodes(algebra_root):
        # rdflib turns LIMIT and OFFSET into a Slice node; `length` is None without LIMIT.
        if node.name != "Slice" or node.length is None:
            continue
        limited_node = node.p
        while limited_node.name in MODIFIERS_ABOVE_PROJECT:
            limited_node = limited_node.p
        # A LIMIT on the outer query only cuts the returned rows, which layer 4 checks.
        if limited_node is top_project or limited_node.name != "Project":
            continue
        cut = f"LIMIT {node.length}" + (f" OFFSET {node.start}" if node.start else "")
        sensitive_names = []
        record_value_names = []
        for returned_variable in limited_node.PV:
            label = query_labels.by_variable.get(str(returned_variable))
            if label == Label.SENSITIVE:
                sensitive_names.append(f"?{returned_variable}")
            if label == Label.RECORD_VALUE:
                record_value_names.append(f"?{returned_variable}")
        subquery_nodes = scope_nodes(limited_node)
        ordered_by_sensitive = False
        aggregates = False
        for subquery_node in subquery_nodes:
            if subquery_node.name == "OrderBy":
                for order_condition in subquery_node.expr:
                    if query_labels.of_expression(order_condition, limited_node) == Label.SENSITIVE:
                        ordered_by_sensitive = True
            if subquery_node.name in ("Group", "AggregateJoin"):
                aggregates = True
        if sensitive_names:
            reason = (f"A subquery returns {', '.join(sensitive_names)} (a patient graph, record or identifier) "
                      f"cut by {cut}: the query works on specific patients only.")
        elif ordered_by_sensitive:
            reason = (f"A subquery is ordered by a patient graph, record or identifier and cut by {cut}: it keeps "
                      "the first patients in that order, so the query works on specific patients only.")
        elif record_value_names and not aggregates:
            reason = (f"A subquery returns raw record values ({', '.join(record_value_names)}) row by row, cut by "
                      f"{cut}: it picks individual records by position, so the query is about specific patients.")
        else:
            continue
        violations.append(Violation(Rule.SINGLE_PATIENT, reason))
    return violations


SPHN_NAMESPACE = "https://biomedit.ch/rdf/sphn-schema/sphn#"
XSD = "http://www.w3.org/2001/XMLSchema#"
SMALL_GROUP_SIZE = 5
QUASI_IDENTIFIER_LIMIT = 3
# Classes of the records that hold an identifying attribute of the patient, and that attribute.
QUASI_IDENTIFIER_CLASSES = {
    URIRef(SPHN_NAMESPACE + "AdministrativeSex"): "sex",
    URIRef(SPHN_NAMESPACE + "Birth"): "birth",
    URIRef(SPHN_NAMESPACE + "BirthDate"): "birth",
    URIRef(SPHN_NAMESPACE + "Nationality"): "nationality",
    URIRef(SPHN_NAMESPACE + "HomeAddress"): "residence",
    URIRef(SPHN_NAMESPACE + "CivilStatus"): "civil status",
    URIRef(SPHN_NAMESPACE + "Death"): "death",
    URIRef(SPHN_NAMESPACE + "DeathDate"): "death",
    URIRef(SPHN_NAMESPACE + "Age"): "age",
}
SPHN_HAS_SUBJECT_AGE = URIRef(SPHN_NAMESPACE + "hasSubjectAge")
SPHN_HAS_UNIT = URIRef(SPHN_NAMESPACE + "hasUnit")
# Steps from a quasi-identifier record to its values (code, quantity, country, date node).
QUASI_IDENTIFIER_DEPTH = 3
DATE_PART_PROPERTIES = {
    URIRef(SPHN_NAMESPACE + "hasYear"): "year",
    URIRef(SPHN_NAMESPACE + "hasMonth"): "month",
    URIRef(SPHN_NAMESPACE + "hasDay"): "day",
}
DATE_PART_FUNCTIONS = {"Builtin_YEAR": "year", "Builtin_MONTH": "month", "Builtin_DAY": "day"}
FULL_DATE = {"year", "month", "day"}
DATE_TYPES = {URIRef(XSD + "dateTime"), URIRef(XSD + "date")}
# Casts and conversions that keep a value recognisable: STR(?a) = "..." fixes ?a.
VALUE_KEEPING_CASTS = {URIRef(XSD + "dateTime"), URIRef(XSD + "date"), URIRef(XSD + "string")}
FULL_DATE_STRING_TESTS = {
    "Builtin_STRSTARTS": ("arg1", "arg2"),
    "Builtin_CONTAINS": ("arg1", "arg2"),
    "Builtin_REGEX": ("text", "pattern"),
}
FLIPPED_OPERATORS = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "=": "="}
BIF_DATEDIFF = "bif:datediff"
# Units of bif:datediff that, compared with a constant date, fix a day or less.
FINE_DATEDIFF_UNITS = {"day", "hour", "minute", "second", "millisecond"}
NEGATED_OPERATORS = {">": "<=", ">=": "<", "<": ">=", "<=": ">"}


def is_full_date_text(text):
    """Return True if a string starts with a full date: "2024-02-12", "2024-02-12T14:06", "^2024-02-12"."""
    text = text.removeprefix("^")
    if len(text) < 10:
        return False
    return text[0:4].isdigit() and text[4] == "-" and text[5:7].isdigit() and text[7] == "-" and text[8:10].isdigit()


def is_date_time_property(property_iri):
    """Return True for the SPHN properties whose values are date-times (sphn:hasAdmissionDateTime...)."""
    return isinstance(property_iri, URIRef) and str(property_iri).startswith(SPHN_NAMESPACE) \
        and str(property_iri).endswith("DateTime")


def path_properties(predicate):
    """Return the plain properties a predicate or property path goes through, in order."""
    if isinstance(predicate, URIRef):
        return [predicate]
    if isinstance(predicate, (SequencePath, AlternativePath)):
        properties = []
        for step in predicate.args:
            properties.extend(path_properties(step))
        return properties
    if isinstance(predicate, MulPath):
        return path_properties(predicate.path)
    if isinstance(predicate, InvPath):
        return path_properties(predicate.arg)
    return []


def last_property(predicate):
    """Return the last plain property of a predicate, or None."""
    properties = path_properties(predicate)
    if not properties:
        return None
    return properties[-1]


def unwrapped(expression):
    """Return the value an expression compares: ?a for STR(?a), xsd:date(?a), xsd:string(?a)."""
    while isinstance(expression, CompValue):
        if expression.name == "Builtin_STR":
            expression = expression.arg
        elif expression.name == "Function" and expression.iri in VALUE_KEEPING_CASTS and len(expression.expr) == 1:
            expression = expression.expr[0]
        else:
            break
    return expression


def is_constant(term):
    return isinstance(term, (URIRef, Literal))


def fixed_day_of(side, other_side):
    """Return the name of the date-time `side = other_side` fixes to one day (or finer), or None.

    - `SUBSTR(STR(?a), 1, n) = "2024-02-12..."` and `STRBEFORE(STR(?a), "T") = "2024-02-12"`:
      the text of the date-time cut to a full date;
    - `bif:datediff("day", ?a, "2024-02-12"^^xsd:date) = 0` (or "hour", "minute", "second"):
      the distance to a constant date, at day precision or finer, fixed to a number.
    """
    if not isinstance(side, CompValue) or not isinstance(other_side, Literal):
        return None
    if side.name in ("Builtin_SUBSTR", "Builtin_STRBEFORE") and is_full_date_text(str(other_side)):
        # rdflib names the text `arg` in SUBSTR and `arg1` in STRBEFORE.
        cut_value = unwrapped(side.get("arg") if side.name == "Builtin_SUBSTR" else side.get("arg1"))
        if isinstance(cut_value, Variable):
            return str(cut_value)
    if side.name == "Function" and str(side.iri) == BIF_DATEDIFF and len(side.expr) == 3:
        unit, first, second = side.expr
        if str(unit).lower() not in FINE_DATEDIFF_UNITS or number(other_side) is None:
            return None
        for date_time, constant in ((first, second), (second, first)):
            date_time = unwrapped(date_time)
            if isinstance(date_time, Variable) and is_constant(constant):
                return str(date_time)
    return None


class FixedValues:
    """What the filters and VALUES of a query fix, collected once.

    exact: names of the variables fixed to one constant (`=`, sameTerm, IN with one item,
        VALUES with one value, or a range whose two ends are the same constant);
    listed: names of the variables restricted to a list of constants (IN, VALUES);
    date_parts: {variable name: {"year", "month", "day"}} fixed with YEAR(?a) = ... ;
    full_date_tests: names of the date-time variables fixed to one day: tested against a full
        date as text (`STRSTARTS(STR(?a), "2024-02-12")`, `SUBSTR(STR(?a), 1, 10) = "2024-02-12"`),
        or at day precision or finer against a constant date (`bif:datediff("day", ?a, "...") = 0`);
    equal_bounds: names of the variables with a range whose two ends are the same constant
        (`?y >= 1975 && ?y <= 1975`), which fixes them like `=`.
    """

    def __init__(self, algebra_root):
        self.exact = set()
        self.listed = set()
        self.date_parts = {}
        self.full_date_tests = set()
        self.equal_bounds = set()
        # {(variable name, ">=" or "<="): constants}
        bounds = {}
        values_by_variable = {}
        for node in all_nodes(algebra_root):
            if node.name == "values":
                for row in node.res:
                    for variable, value in row.items():
                        if is_constant(value):
                            values_by_variable.setdefault(str(variable), set()).add(value)
            elif node.name == "RelationalExpression":
                self.add_comparison(node, bounds)
            elif node.name == "Builtin_sameTerm":
                self.add_equality([node.arg1, node.arg2])
            elif node.name in FULL_DATE_STRING_TESTS:
                tested_field, constant_field = FULL_DATE_STRING_TESTS[node.name]
                tested, constant = unwrapped(node[tested_field]), node[constant_field]
                if isinstance(tested, Variable) and isinstance(constant, Literal) and is_full_date_text(str(constant)):
                    self.full_date_tests.add(str(tested))
        for name, values in values_by_variable.items():
            self.listed.add(name)
            if len(values) == 1:
                self.exact.add(name)
        for (name, operator), constants in bounds.items():
            if operator == ">=" and constants & bounds.get((name, "<="), set()):
                self.equal_bounds.add(name)
        self.exact |= self.equal_bounds

    def add_equality(self, sides):
        """Record `side = constant`: the variable (or the date part) on the other side is fixed."""
        if not any(is_constant(side) for side in sides):
            return
        for side, other_side in ((sides[0], sides[1]), (sides[1], sides[0])):
            date_time = fixed_day_of(side, other_side)
            if date_time is not None:
                self.full_date_tests.add(date_time)
        for side in sides:
            value = unwrapped(side)
            if isinstance(value, Variable):
                self.exact.add(str(value))
            if isinstance(side, CompValue) and side.name in DATE_PART_FUNCTIONS:
                date_value = unwrapped(side.arg)
                if isinstance(date_value, Variable):
                    self.date_parts.setdefault(str(date_value), set()).add(DATE_PART_FUNCTIONS[side.name])

    def add_comparison(self, node, bounds):
        operator = str(node.op)
        if operator == "=":
            self.add_equality([node.expr, node.other])
        elif operator == "IN" and isinstance(node.other, list):
            value = unwrapped(node.expr)
            if isinstance(value, Variable) and node.other and all(is_constant(item) for item in node.other):
                self.listed.add(str(value))
                if len(node.other) == 1:
                    self.exact.add(str(value))
        elif operator in ("<=", ">="):
            left, right = unwrapped(node.expr), unwrapped(node.other)
            if isinstance(right, Variable) and is_constant(left):
                left, right, operator = right, left, FLIPPED_OPERATORS[operator]
            if isinstance(left, Variable) and is_constant(right):
                bounds.setdefault((str(left), operator), set()).add(right)


def count_bound(comparison, count_names, negated):
    """Return the largest group size a comparison keeps, or None if it does not compare a count.

    `count_names` are the names of the variables that hold a count in the comparison's scope.
    """
    left, operator, right = comparison.expr, str(comparison.op), comparison.other
    if operator == "IN":
        if negated or not isinstance(left, Variable) or str(left) not in count_names:
            return None
        if not isinstance(right, list) or not right:
            return None
        sizes = [number(item) for item in right]
        if None in sizes:
            return None
        return max(sizes)
    if isinstance(right, Variable) and str(right) in count_names and number(left) is not None:
        left, right, operator = right, left, FLIPPED_OPERATORS.get(operator, operator)
    if not (isinstance(left, Variable) and str(left) in count_names and number(right) is not None):
        return None
    if negated:
        operator = NEGATED_OPERATORS.get(operator)
    size = number(right)
    if operator == "=":
        return size
    if operator == "<":
        return size - 1
    if operator == "<=":
        return size
    return None


def number(term):
    """Return the numeric value of a literal, or None."""
    if not isinstance(term, Literal):
        return None
    value = term.toPython()
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def small_group_sizes(expression, count_names, negated=False):
    """Return the sizes of the small groups (at most SMALL_GROUP_SIZE) a filter expression keeps."""
    sizes = []
    if isinstance(expression, list):
        for item in expression:
            sizes.extend(small_group_sizes(item, count_names, negated))
        return sizes
    if not isinstance(expression, CompValue):
        return sizes
    if expression.name == "UnaryNot":
        return small_group_sizes(expression.expr, count_names, not negated)
    if expression.name == "RelationalExpression":
        size = count_bound(expression, count_names, negated)
        if size is not None and 0 <= size <= SMALL_GROUP_SIZE:
            sizes.append(size)
    for field_name, field_value in expression.items():
        if field_name != "_vars":
            sizes.extend(small_group_sizes(field_value, count_names, negated))
    return sizes


def small_group_violations(algebra_root):
    """Report HAVING clauses (and filters on counts) that keep only groups of a few patients."""
    scopes = query_scopes(algebra_root)
    # A count's alias (COUNT(...) AS ?n) is the same variable wherever it is used.
    count_aliases = set()
    count_results_by_scope = {}
    for scope in scopes:
        count_results = set()
        nodes = scope_nodes(scope)
        for node in nodes:
            if node.name == "AggregateJoin":
                for aggregate in node.A:
                    if aggregate.name == "Aggregate_Count":
                        count_results.add(str(aggregate.res))
        for node in nodes:
            if node.name == "Extend" and isinstance(node.expr, Variable) and str(node.expr) in count_results:
                count_aliases.add(str(node.var))
        count_results_by_scope[id(scope)] = count_results

    sizes = []
    for scope in scopes:
        count_names = count_results_by_scope[id(scope)] | count_aliases
        nodes = scope_nodes(scope)
        for node in nodes:
            # rdflib turns `HAVING (?n = 1)` into a SAMPLE of the alias.
            if node.name == "AggregateJoin":
                for aggregate in node.A:
                    if aggregate.name == "Aggregate_Sample" and str(aggregate.vars) in count_aliases:
                        count_names.add(str(aggregate.res))
        for node in nodes:
            if node.name == "Filter":
                sizes.extend(small_group_sizes(node.expr, count_names))
    if not sizes:
        return []
    reason = (f"The query keeps only groups of at most {int(max(sizes))} patients (HAVING on a small count): "
              "each result points at a handful of patients, which counts as a single patient.")
    return [Violation(Rule.SMALL_GROUP, reason)]


def patient_patterns(algebra_root):
    """Return the triple patterns of the query that are not on the terminology graph."""
    patterns = []
    for graph_term, triple in triple_patterns_with_graph(algebra_root):
        if not is_terminology_graph(graph_term):
            patterns.append(triple)
    return patterns


def value_aliases(algebra_root):
    """Return {alias name: source name} for BIND(?a AS ?x) and BIND(STR(?a) AS ?x)."""
    aliases = {}
    for node in all_nodes(algebra_root):
        if node.name == "Extend" and node.var is not None:
            source = unwrapped(node.expr)
            if isinstance(source, Variable):
                aliases[str(node.var)] = str(source)
    return aliases


def resolved(name, aliases):
    """Follow BIND aliases back to the variable a pattern binds."""
    seen = set()
    while name in aliases and name not in seen:
        seen.add(name)
        name = aliases[name]
    return name


def exact_date_violations(algebra_root, fixed_values):
    """Report exact timestamps and full dates, in triple patterns or in filters."""
    patterns = patient_patterns(algebra_root)
    aliases = value_aliases(algebra_root)
    date_time_names = set()
    # {date node: {"year", "month", "day"}} fixed for that date node.
    fixed_parts_by_date_node = {}
    for subject, predicate, triple_object in patterns:
        final_property = last_property(predicate)
        if is_date_time_property(final_property):
            if isinstance(triple_object, Variable):
                date_time_names.add(str(triple_object))
            elif isinstance(triple_object, Literal) and (
                    triple_object.datatype in DATE_TYPES or is_full_date_text(str(triple_object))):
                reason = (f"Triple pattern fixes {predicate.n3()} to the exact timestamp {triple_object.n3()}: "
                          "it singles out a patient. Use a range or a year or month instead.")
                return [Violation(Rule.EXACT_DATE, reason)]
        if final_property in DATE_PART_PROPERTIES:
            part = DATE_PART_PROPERTIES[final_property]
            fixed = is_constant(triple_object)
            if isinstance(triple_object, Variable):
                fixed = str(triple_object) in fixed_values.exact
            if fixed:
                fixed_parts_by_date_node.setdefault(subject, set()).add(part)

    fixed_names = set()
    for name in fixed_values.exact | fixed_values.listed | fixed_values.equal_bounds:
        fixed_names.add(resolved(name, aliases))
    fixed_date_times = sorted(fixed_names & date_time_names)
    if fixed_date_times:
        reason = (f"A filter fixes ?{fixed_date_times[0]} to an exact timestamp: it singles out a patient. "
                  "Use a range or a year or month instead.")
        return [Violation(Rule.EXACT_DATE, reason)]

    full_date_names = set()
    for name, parts in fixed_values.date_parts.items():
        if parts == FULL_DATE:
            full_date_names.add(resolved(name, aliases))
    for name in fixed_values.full_date_tests:
        full_date_names.add(resolved(name, aliases))
    full_date_names &= date_time_names
    full_date_nodes = [node for node, parts in fixed_parts_by_date_node.items() if parts == FULL_DATE]
    if full_date_names or full_date_nodes:
        reason = ("The query fixes a full date (year, month and day): it singles out a patient. "
                  "Use a range or a year or month instead.")
        return [Violation(Rule.EXACT_DATE, reason)]
    return []


def quasi_identifier_records(patterns):
    """Return {record variable: attribute} for the records that hold an identifying attribute."""
    records = {}
    for subject, predicate, triple_object in patterns:
        if not isinstance(subject, Variable):
            continue
        if predicate == RDF.type and triple_object in QUASI_IDENTIFIER_CLASSES:
            records[subject] = QUASI_IDENTIFIER_CLASSES[triple_object]
        if predicate == SPHN_HAS_SUBJECT_AGE and isinstance(triple_object, Variable):
            records[triple_object] = "age"
    return records


def fixes_value(subject, predicate, triple_object, fixed_names):
    """Return True if a triple pattern of a quasi-identifier record pins the record's value."""
    if SPHN_HAS_UNIT in path_properties(predicate):
        return False
    if predicate == RDF.type and isinstance(triple_object, URIRef) and str(triple_object).startswith(SPHN_NAMESPACE):
        # `?q a sphn:Quantity`: the structure of the record, not a value.
        return False
    if is_constant(triple_object):
        return True
    return isinstance(triple_object, Variable) and str(triple_object) in fixed_names


def quasi_identifier_violations(algebra_root, fixed_values):
    """Report queries that fix QUASI_IDENTIFIER_LIMIT or more identifying attributes to exact values."""
    patterns = patient_patterns(algebra_root)
    aliases = value_aliases(algebra_root)
    fixed_names = set()
    for name in fixed_values.exact:
        fixed_names.add(resolved(name, aliases))
    fixed_attributes = set()
    for record, attribute in quasi_identifier_records(patterns).items():
        record_nodes = {record}
        frontier = {record}
        for _ in range(QUASI_IDENTIFIER_DEPTH):
            next_frontier = set()
            for subject, predicate, triple_object in patterns:
                if subject in frontier and isinstance(triple_object, Variable) and triple_object not in record_nodes:
                    next_frontier.add(triple_object)
            record_nodes |= next_frontier
            frontier = next_frontier
        for subject, predicate, triple_object in patterns:
            if subject not in record_nodes or (subject == record and predicate == RDF.type):
                continue
            if fixes_value(subject, predicate, triple_object, fixed_names):
                fixed_attributes.add(attribute)
                break
    # `?dx sphn:hasSubjectAge/sphn:hasQuantity/sphn:hasValue ?age` with ?age fixed.
    for subject, predicate, triple_object in patterns:
        if SPHN_HAS_SUBJECT_AGE in path_properties(predicate) and predicate != SPHN_HAS_SUBJECT_AGE:
            if fixes_value(subject, predicate, triple_object, fixed_names):
                fixed_attributes.add("age")
    if len(fixed_attributes) < QUASI_IDENTIFIER_LIMIT:
        return []
    reason = (f"The query fixes {len(fixed_attributes)} identifying attributes to exact values "
              f"({', '.join(sorted(fixed_attributes))}): together they can single out one patient. "
              "Use ranges or group by these attributes instead.")
    return [Violation(Rule.QUASI_IDENTIFIERS, reason)]
