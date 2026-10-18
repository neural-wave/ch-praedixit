"""Layer 1 of the structural checks: query form, dataset clauses, federation and functions.

These checks need no knowledge of the SPHN data. They accept only the SPARQL features the
generator is expected to use and report everything else.
"""

from rdflib import URIRef

from pppp.checker.GateB.algebra_walk import all_nodes
from pppp.checker.GateB.data_layout import PATIENT_GRAPH_PREFIX
from pppp.checker.GateB.rules import Rule, Violation

XSD = "http://www.w3.org/2001/XMLSchema#"
# Function calls by IRI that are allowed: Virtuoso's date difference and the standard XSD casts.
ALLOWED_FUNCTION_IRIS = {
    URIRef("bif:datediff"),
    URIRef(XSD + "string"),
    URIRef(XSD + "boolean"),
    URIRef(XSD + "integer"),
    URIRef(XSD + "decimal"),
    URIRef(XSD + "float"),
    URIRef(XSD + "double"),
    URIRef(XSD + "dateTime"),
}
QUERY_FORM_NAMES = {
    "AskQuery": "ASK",
    "ConstructQuery": "CONSTRUCT",
    "DescribeQuery": "DESCRIBE",
}


def query_form_violations(algebra_root):
    """Report a query that is not a SELECT."""
    if algebra_root.name == "SelectQuery":
        return []
    query_form = QUERY_FORM_NAMES.get(algebra_root.name, algebra_root.name)
    return [Violation(Rule.NOT_SELECT, f"{query_form} queries are not allowed; only SELECT is.")]


def dataset_violations(algebra_root):
    """Report FROM and FROM NAMED clauses.

    A clause on a patient graph restricts the query to one patient. Any other clause is
    outside the allow list: patient graphs are reached with GRAPH ?g over the whole dataset.
    """
    violations = []
    dataset_clauses = algebra_root.get("datasetClause") or []
    for dataset_clause in dataset_clauses:
        if "named" in dataset_clause:
            clause_keyword = "FROM NAMED"
            graph_iri = dataset_clause.named
        else:
            clause_keyword = "FROM"
            graph_iri = dataset_clause.default
        if str(graph_iri).startswith(PATIENT_GRAPH_PREFIX):
            reason = f"'{clause_keyword} <{graph_iri}>' restricts the query to one patient graph."
            violations.append(Violation(Rule.SINGLE_PATIENT, reason))
        else:
            reason = f"'{clause_keyword} <{graph_iri}>' is not allowed: the query must run on the whole dataset."
            violations.append(Violation(Rule.NOT_READ_ONLY, reason))
    return violations


def service_violations(algebra_root):
    """Report SERVICE clauses, which send query bindings to another endpoint."""
    violations = []
    for node in all_nodes(algebra_root):
        if node.name != "ServiceGraphPattern":
            continue
        reason = f"'SERVICE <{node.term}>' sends data to an external endpoint."
        violations.append(Violation(Rule.NOT_READ_ONLY, reason))
    return violations


def function_violations(algebra_root):
    """Report calls to functions outside the allow list (standard built-ins are always allowed)."""
    violations = []
    reported_function_iris = set()
    for node in all_nodes(algebra_root):
        if node.name != "Function":
            continue
        if node.iri in ALLOWED_FUNCTION_IRIS or node.iri in reported_function_iris:
            continue
        reported_function_iris.add(node.iri)
        reason = f"Function '{node.iri}' is not allowed: server-side functions can read or write outside the query."
        violations.append(Violation(Rule.NOT_READ_ONLY, reason))
    return violations
