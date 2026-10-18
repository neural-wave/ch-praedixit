"""Layer 2 of the structural checks: triple patterns and constant IRIs.

- A negated property path (`?s !rdf:type ?o`) matches any property, i.e. the whole graph.
- Every triple pattern must be restricted: by a fixed property, class, subject or object, or
  through a shared variable by a restricted pattern. `?s ?p ?o`, `?s a ?c` and `?s (p|^p)* ?o`
  (which relates every node to itself) match the whole graph on their own, but
  `?dx a sphn:BilledDiagnosis . ?dx ?p ?o` only lists the properties of the diagnoses. A
  restriction inside OPTIONAL, MINUS or NOT EXISTS, or in another UNION branch, does not
  restrict the pattern.
- Constant IRIs must not point at patient data. A patient graph IRI or the IRI of a patient
  record restricts the query to one patient, wherever it appears (GRAPH, VALUES, FILTER...).

Record IRIs and terminology IRIs share the namespace `https://biomedit.ch/rdf/sphn-resource/`:
terminology classes sit under a path segment naming the terminology
(`.../sphn-resource/icd-10-gm/A00`), records under a data provider code
(`.../sphn-resource/CHE-108_904_325-sphn-AdministrativeCase-...`).
"""

from rdflib import RDF, Literal, URIRef, Variable
from rdflib.paths import AlternativePath, InvPath, MulPath, NegatedPath, SequencePath
from rdflib.plugins.sparql.parserutils import CompValue

from pppp.checker.GateB.algebra_walk import all_nodes, child_nodes, constant_bound_variables
from pppp.checker.GateB.data_layout import PATIENT_GRAPH_PREFIX, TERMINOLOGY_GRAPH
from pppp.checker.GateB.rules import Rule, Violation

SPHN_RESOURCE_NAMESPACE = "https://biomedit.ch/rdf/sphn-resource/"
# Path segments under SPHN_RESOURCE_NAMESPACE that hold terminologies, not patient records.
TERMINOLOGY_SEGMENTS = {
    "atc", "chop", "dcm", "eco", "edam", "efo", "einstein", "emdn", "genepio", "geno",
    "hgnc", "icd-10-gm", "loinc", "obi", "ordo", "so", "ucum",
}


def triple_patterns(algebra_root):
    """Return every (subject, predicate, object) triple pattern of the query, subqueries included."""
    patterns = []
    for node in all_nodes(algebra_root):
        if node.name == "BGP":
            patterns.extend(node.triples)
    return patterns


def path_matches_any_property(path):
    """Return True if a property path contains a negated property set (`!p`)."""
    if isinstance(path, NegatedPath):
        return True
    if isinstance(path, (AlternativePath, SequencePath)):
        for sub_path in path.args:
            if path_matches_any_property(sub_path):
                return True
        return False
    if isinstance(path, MulPath):
        return path_matches_any_property(path.path)
    if isinstance(path, InvPath):
        return path_matches_any_property(path.arg)
    return False


def path_can_be_empty(path):
    """Return True if a property path can take zero steps (`p*`, `p?`): it relates every node to itself."""
    if isinstance(path, MulPath):
        return path.mod in ("*", "?") or path_can_be_empty(path.path)
    if isinstance(path, SequencePath):
        return all(path_can_be_empty(step) for step in path.args)
    if isinstance(path, AlternativePath):
        return any(path_can_be_empty(alternative) for alternative in path.args)
    if isinstance(path, InvPath):
        return path_can_be_empty(path.arg)
    return False


def is_restricting(triple):
    """Return True if a triple pattern restricts what it matches on its own.

    A fixed subject or object restricts it; so does a fixed property, except rdf:type with a
    variable class (`?s a ?c` matches every typed node) and paths that can take zero steps.
    """
    subject, predicate, triple_object = triple
    if isinstance(subject, (URIRef, Literal)) or isinstance(triple_object, (URIRef, Literal)):
        return True
    if isinstance(predicate, URIRef):
        return predicate != RDF.type
    if isinstance(predicate, Variable):
        return False
    return not path_matches_any_property(predicate) and not path_can_be_empty(predicate)


def scoped_triple_patterns(node, optional=False, branch=(), union_counter=None):
    """Return every triple pattern below `node` as (triple, optional, branch).

    optional: the pattern is inside OPTIONAL, MINUS or NOT EXISTS, so it cannot restrict the rest;
    branch: the UNION branches the pattern sits in, as a tuple of (union number, 0 or 1).
    """
    if union_counter is None:
        union_counter = [0]
    if node.name == "BGP":
        return [(triple, optional, branch) for triple in node.triples]
    patterns = []
    if node.name in ("LeftJoin", "Minus"):
        patterns.extend(scoped_triple_patterns(node.p1, optional, branch, union_counter))
        patterns.extend(scoped_triple_patterns(node.p2, True, branch, union_counter))
        if node.name == "LeftJoin" and isinstance(node.get("expr"), CompValue):
            patterns.extend(scoped_triple_patterns(node.expr, optional, branch, union_counter))
        return patterns
    if node.name == "Union":
        union_counter[0] += 1
        union_number = union_counter[0]
        patterns.extend(scoped_triple_patterns(node.p1, optional, branch + ((union_number, 0),), union_counter))
        patterns.extend(scoped_triple_patterns(node.p2, optional, branch + ((union_number, 1),), union_counter))
        return patterns
    if node.name == "Builtin_NOTEXISTS":
        optional = True
    for child in child_nodes(node):
        patterns.extend(scoped_triple_patterns(child, optional, branch, union_counter))
    return patterns


def triple_variables(triple):
    return {str(term) for term in triple if isinstance(term, Variable)}


def unrestricted_patterns(algebra_root):
    """Return the triple patterns that no restricted pattern restricts (see module docstring)."""
    patterns = scoped_triple_patterns(algebra_root)
    constant_names = constant_bound_variables(algebra_root)
    unrestricted = []
    for current_branch in {branch for triple, optional, branch in patterns}:
        # The patterns of this branch and of the parts of the query around it.
        visible = []
        for triple, optional, branch in patterns:
            if branch == current_branch[:len(branch)]:
                visible.append((triple, optional))
        restricted_names = set(constant_names)
        for triple, optional in visible:
            if is_restricting(triple) and not optional:
                restricted_names |= triple_variables(triple)
        # A join spreads the restriction: ?dx a sphn:BilledDiagnosis . ?dx ?p ?o
        names_changed = True
        while names_changed:
            names_changed = False
            for triple, optional in visible:
                names = triple_variables(triple)
                if names & restricted_names and not names <= restricted_names:
                    restricted_names |= names
                    names_changed = True
        for triple, optional, branch in patterns:
            if branch != current_branch or is_restricting(triple) or triple_variables(triple) & restricted_names:
                continue
            if triple not in unrestricted:
                unrestricted.append(triple)
    return unrestricted


def predicate_violations(algebra_root):
    """Report negated property paths and triple patterns that match the whole graph."""
    violations = []
    for subject, predicate, triple_object in triple_patterns(algebra_root):
        if isinstance(predicate, Variable) or not path_matches_any_property(predicate):
            continue
        pattern_text = f"{subject.n3()} {predicate.n3()} {triple_object.n3()}"
        reason = f"Property path in '{pattern_text}' is negated: it matches any property except the listed ones."
        violations.append(Violation(Rule.UNRESTRICTED_MATCH, reason))
    for subject, predicate, triple_object in unrestricted_patterns(algebra_root):
        if not isinstance(predicate, Variable) and path_matches_any_property(predicate):
            continue  # already reported as a negated path
        pattern_text = f"{subject.n3()} {predicate.n3()} {triple_object.n3()}"
        if isinstance(predicate, Variable):
            reason = (f"Triple pattern '{pattern_text}' has a variable predicate and is not tied to any class or "
                      "property restriction: it matches any property of the graph.")
        else:
            reason = (f"Triple pattern '{pattern_text}' is not tied to any class or property restriction: "
                      "it matches the whole graph.")
        violations.append(Violation(Rule.UNRESTRICTED_MATCH, reason))
    return violations


def iris_in(value):
    """Return the constant IRIs found in an algebra value: a node, a list, a tuple, a dict or a term."""
    iris = []
    if isinstance(value, URIRef):
        iris.append(value)
    elif isinstance(value, CompValue):
        for field_name, field_value in value.items():
            # `_vars` is rdflib's variable cache; FROM clauses are checked by layer 1.
            if field_name in ("_vars", "datasetClause"):
                continue
            iris.extend(iris_in(field_value))
    elif isinstance(value, (list, tuple)):
        for item in value:
            iris.extend(iris_in(item))
    elif isinstance(value, dict):
        # VALUES rows: {variable: term}.
        for term in value.values():
            iris.extend(iris_in(term))
    return iris


def is_patient_record_iri(iri):
    """Return True if an IRI under the SPHN resource namespace is a record, not a terminology."""
    # rdflib terms never compare equal to plain strings, so compare the IRI text.
    iri_text = str(iri)
    if not iri_text.startswith(SPHN_RESOURCE_NAMESPACE):
        return False
    if iri_text == TERMINOLOGY_GRAPH:
        return False
    first_segment = iri_text[len(SPHN_RESOURCE_NAMESPACE):].split("/")[0]
    return first_segment not in TERMINOLOGY_SEGMENTS


def constant_iri_violations(algebra_root):
    """Report constant IRIs of patient graphs and patient records, once per IRI."""
    violations = []
    reported_iris = set()
    for iri in iris_in(algebra_root):
        if iri in reported_iris:
            continue
        if iri.startswith(PATIENT_GRAPH_PREFIX):
            reason = f"IRI <{iri}> is a patient graph: it restricts the query to one patient."
        elif is_patient_record_iri(iri):
            reason = f"IRI <{iri}> is a patient record: it restricts the query to one patient."
        else:
            continue
        reported_iris.add(iri)
        violations.append(Violation(Rule.SINGLE_PATIENT, reason))
    return violations
