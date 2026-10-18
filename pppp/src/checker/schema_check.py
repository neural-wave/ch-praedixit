"""Schema check of a generated query: does it use classes and properties the patient data contains?

The system has no access to Virtuoso, so a query cannot be tried on the data. Instead it is
compared with the schema the mock patients actually use (data/sphn/data_schema.json, written by
scripts/schema/extract_data_schema.py). A query that names a property no record carries is valid
SPARQL and may well be compliant, but it can only return nothing: the translator made a mistake.

`schema_problems(query_text)` returns those mistakes:
- a sphn: class that types no record (and none of its subclasses does either);
- a sphn: property that no record carries, with the closest names that exist;
- a property used on a record whose class never carries it, with the properties it does carry
  (`?d a sphn:BilledDiagnosis ; sphn:hasMethodCode ?m`).

The problems never change the compliance verdict; they only trigger a retry of the translation
(see verdict.should_retry_translation) and tell the translator what to fix (verdict.retry_feedback).
A query that does not parse has no problems here: Gate B reports it as invalid_sparql.
"""

import dataclasses
import difflib
import functools
import json
import pathlib

from rdflib import RDF, URIRef, Variable
from rdflib.paths import SequencePath

from pppp.checker.GateB.parsing import parse_request
from pppp.checker.GateB.sensitivity_checks import path_properties
from pppp.checker.GateB.variable_labels import is_terminology_graph, triple_patterns_with_graph
from pppp.checker.GateB.virtuoso_extensions import scan_virtuoso_extensions

SPHN = "https://biomedit.ch/rdf/sphn-schema/sphn#"
# This file is at <repository>/pppp/src/checker/schema_check.py.
REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_DATA_SCHEMA_PATH = REPOSITORY_ROOT / "data" / "sphn" / "data_schema.json"
# How many existing properties a problem lists, so that the retry prompt stays short.
LISTED_PROPERTIES = 15


@dataclasses.dataclass(frozen=True)
class SchemaProblem:
    """A class or property of the query that the data does not contain, with a hint for the retry."""

    term: str
    message: str


@dataclasses.dataclass(frozen=True)
class DataSchema:
    """The schema the patient data uses (see the module docstring for where it comes from)."""

    classes: frozenset
    properties: frozenset
    class_properties: dict
    covered_by: dict

    def record_classes(self, schema_class):
        """Return the data classes whose records a pattern `?x a sphn:<schema_class>` can match."""
        found = set(self.covered_by.get(schema_class, ()))
        if schema_class in self.classes:
            found.add(schema_class)
        return found


@functools.lru_cache(maxsize=None)
def load_data_schema(path=DEFAULT_DATA_SCHEMA_PATH):
    """Read data_schema.json (cached per path) and return a DataSchema."""
    content = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    return DataSchema(
        classes=frozenset(content["classes"]),
        properties=frozenset(content["properties"]),
        class_properties={name: frozenset(properties) for name, properties in content["class_properties"].items()},
        covered_by={name: frozenset(classes) for name, classes in content["covered_by"].items()},
    )


def sphn_name(term):
    """Return the local name of a sphn: IRI, or None for anything else."""
    if isinstance(term, URIRef) and str(term).startswith(SPHN):
        return str(term)[len(SPHN):]
    return None


def first_step(predicate):
    """Return the property a predicate starts with from its subject: `p` for `p` and for `p/q`, else None."""
    if isinstance(predicate, URIRef):
        return predicate
    if isinstance(predicate, SequencePath) and isinstance(predicate.args[0], URIRef):
        return predicate.args[0]
    return None


def schema_problems(query_text, data_schema=None):
    """Return the SchemaProblem list of a query (empty when the query fits the data, or does not parse)."""
    data_schema = data_schema or load_data_schema()
    parsed_request = parse_request(scan_virtuoso_extensions(query_text).standard_text)
    if parsed_request.algebra is None:
        return []
    patterns = []
    for graph_term, triple in triple_patterns_with_graph(parsed_request.algebra):
        if not is_terminology_graph(graph_term):
            patterns.append(triple)

    problems = {}

    def report(term, message):
        problems.setdefault((term, message), SchemaProblem(term, message))

    # The classes each subject variable is given by `?x a sphn:<class>`.
    classes_by_subject = {}
    for subject, predicate, triple_object in patterns:
        class_name = sphn_name(triple_object)
        if predicate != RDF.type or class_name is None:
            continue
        record_classes = data_schema.record_classes(class_name)
        if not record_classes:
            report(f"sphn:{class_name}", f"No record in the data has the class sphn:{class_name}.")
            continue
        if isinstance(subject, Variable):
            classes_by_subject.setdefault(str(subject), set()).update(record_classes)

    for subject, predicate, triple_object in patterns:
        for step in path_properties(predicate):
            property_name = sphn_name(step)
            if property_name is None or property_name in data_schema.properties:
                continue
            close = difflib.get_close_matches(property_name, data_schema.properties, n=3, cutoff=0.6)
            hint = f" Did you mean {', '.join('sphn:' + name for name in close)}?" if close else ""
            report(f"sphn:{property_name}", f"No record in the data carries sphn:{property_name}.{hint}")
        property_name = sphn_name(first_step(predicate))
        subject_classes = classes_by_subject.get(str(subject)) if isinstance(subject, Variable) else None
        if property_name is None or property_name not in data_schema.properties or not subject_classes:
            continue
        carried = set()
        for record_class in subject_classes:
            carried |= data_schema.class_properties.get(record_class, frozenset())
        if property_name in carried:
            continue
        class_text = " or ".join(f"sphn:{name}" for name in sorted(subject_classes))
        listed = ", ".join(f"sphn:{name}" for name in sorted(carried)[:LISTED_PROPERTIES])
        more = ", ..." if len(carried) > LISTED_PROPERTIES else ""
        report(f"?{subject} sphn:{property_name}",
               f"Records of {class_text} never carry sphn:{property_name}; they carry {listed}{more}.")
    return list(problems.values())
