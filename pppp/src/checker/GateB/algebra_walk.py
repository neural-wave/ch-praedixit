"""Navigation of the SPARQL algebra tree that rdflib builds for a query.

Each node is a `CompValue` whose `name` is the operator (`Project`, `BGP`, `Graph`,
`Extend`, `Function`, ...) and whose fields hold sub-nodes, lists of sub-nodes, or RDF terms.
Expressions (filters, BIND, aggregates) are nodes of the same tree.
"""

from rdflib import Literal, URIRef, Variable
from rdflib.plugins.sparql.parserutils import CompValue


def child_nodes(node):
    """Return the nodes directly contained in `node`, in field order."""
    children = []
    for field_name, field_value in node.items():
        # `_vars` is rdflib's cache of the variables used below the node, not a sub-node.
        if field_name == "_vars":
            continue
        if isinstance(field_value, CompValue):
            children.append(field_value)
            continue
        if isinstance(field_value, list):
            for item in field_value:
                if isinstance(item, CompValue):
                    children.append(item)
    return children


def query_scopes(algebra_root):
    """Return the Project nodes of a query: the top-level SELECT first, then each subquery.

    A query without Project node (e.g. ASK) is returned as its own single scope.
    """
    scopes = []
    for node in all_nodes(algebra_root):
        if node.name == "Project":
            scopes.append(node)
    if not scopes:
        scopes.append(algebra_root)
    return scopes


def scope_nodes(scope):
    """Return the nodes below a Project node that do not belong to a nested subquery."""
    nodes = []
    nodes_to_visit = list(child_nodes(scope))
    while nodes_to_visit:
        node = nodes_to_visit.pop()
        if node.name == "Project":
            continue
        nodes.append(node)
        nodes_to_visit.extend(child_nodes(node))
    return nodes


def all_nodes(root):
    """Return `root` and every node below it, subqueries and expressions included."""
    nodes = []
    nodes_to_visit = [root]
    while nodes_to_visit:
        node = nodes_to_visit.pop()
        nodes.append(node)
        nodes_to_visit.extend(child_nodes(node))
    return nodes


def variables_in_expression(expression):
    """Return the names of the variables used in an expression (a node, a list or a term)."""
    if isinstance(expression, Variable):
        return {str(expression)}
    names = set()
    if isinstance(expression, CompValue):
        for field_name, field_value in expression.items():
            if field_name == "_vars":
                continue
            names |= variables_in_expression(field_value)
    if isinstance(expression, list):
        for item in expression:
            names |= variables_in_expression(item)
    return names


def constant_bound_variables(algebra_root):
    """Return the names of the variables that VALUES or BIND only ever set to constants.

    `VALUES ?pid { "1000042" }` and `BIND ("1000042" AS ?pid)` make ?pid a constant in disguise.
    """
    constant_names = set()
    non_constant_names = set()
    for node in all_nodes(algebra_root):
        if node.name == "values":
            for row in node.res:
                for variable, value in row.items():
                    if isinstance(value, (URIRef, Literal)):
                        constant_names.add(str(variable))
                    else:
                        non_constant_names.add(str(variable))
        if node.name == "Extend" and node.var is not None:
            if isinstance(node.expr, (URIRef, Literal)):
                constant_names.add(str(node.var))
            else:
                non_constant_names.add(str(node.var))
    return constant_names - non_constant_names
