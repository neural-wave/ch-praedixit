"""Vocabulary of the SPHN schema 2026.1 that the structural checks need.

Read from the schema (OWL) and from the SHACL shapes:
- identifier properties: datatype properties whose name contains "Identifier"
  (sphn:hasIdentifier, sphn:hasSharedIdentifier, ...); their values identify a record;
- datatype properties: properties whose values are literals of a record (dates,
  measurements, free texts...);
- value-set properties: object properties whose values are named individuals of a value set
  (a subclass of sphn:ValueSet), e.g. sphn:hasCoverageType -> sphn-individual:Private;
- value sets per class: the same decision for each (class, property) of the SHACL shapes. A
  property can point at a value set on one class and at records on another: sphn:hasAlgorithm
  takes sphn:Hash_algorithm individuals on sphn:Hash, sphn:DataCompressionAlgorithm records on
  the imaging frames. When the query fixes the class of a subject, this decision is used.

The mock patient data follows schema 2024.2 and uses some properties that 2026.1 does not
define. The literal-valued ones are listed in DATATYPE_PROPERTIES_2024_2, those pointing at
value-set individuals in VALUE_SET_PROPERTIES_2024_2; both are added to the sets above. The
other undefined properties point at record nodes (e.g. sphn:hasDischargeLocation) and are in
none of these sets: the checks treat their values as record IRIs.
"""

import dataclasses
import functools
import pathlib

from rdflib import Graph, URIRef

# This file is at <repository>/pppp/src/checker/GateB/vocabulary.py.
REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[4]
DEFAULT_SCHEMA_PATH = REPOSITORY_ROOT / "data" / "sphn" / "sphn_rdf_schema.ttl"
DEFAULT_SHACL_PATH = REPOSITORY_ROOT / "data" / "sphn" / "shacler_shapes.ttl"

SPHN_NAMESPACE = "https://biomedit.ch/rdf/sphn-schema/sphn#"
# Literal-valued properties of schema 2024.2 that the mock data uses and 2026.1 does not define
# (2026.1 models admission and discharge as sphn:Admission / sphn:Discharge nodes with
# sphn:hasDateTime). Found by scanning 1,000 random mock patient files for sphn: properties
# missing from the 2026.1 schema and keeping those whose objects are literals.
DATATYPE_PROPERTIES_2024_2 = frozenset({
    URIRef(SPHN_NAMESPACE + "hasAdmissionDateTime"),
    URIRef(SPHN_NAMESPACE + "hasDischargeDateTime"),
    URIRef(SPHN_NAMESPACE + "hasFirstAdministrationDateTime"),
    URIRef(SPHN_NAMESPACE + "hasLastAdministrationDateTime"),
    URIRef(SPHN_NAMESPACE + "hasGenomicSequence"),
})
# Properties of schema 2024.2 that the mock data uses and 2026.1 does not define, whose values
# are named individuals of a value set (e.g. sphn-individual:Principal, sphn-individual:Elongated).
# 2026.1 replaces them with coded properties (sphn:hasRankCode, sphn:hasCellMorphologyCode).
VALUE_SET_PROPERTIES_2024_2 = frozenset({
    URIRef(SPHN_NAMESPACE + "hasRank"),
    URIRef(SPHN_NAMESPACE + "hasCellMorphology"),
})

DATATYPE_PROPERTIES_QUERY = """
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>
SELECT ?property WHERE {
  ?property a owl:DatatypeProperty .
  FILTER (STRSTARTS(STR(?property), STR(sphn:)))
}
"""
VALUE_SET_CLASSES_QUERY = """
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>
SELECT ?value_set_class WHERE { ?value_set_class rdfs:subClassOf+ sphn:ValueSet . }
"""
# Classes a property may point to, from `sh:class` and from the alternatives of `sh:or`.
PROPERTY_CLASSES_QUERY = """
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX sh: <http://www.w3.org/ns/shacl#>
SELECT ?subject_class ?property ?target_class WHERE {
  ?node_shape sh:targetClass ?subject_class ;
              sh:property ?property_shape .
  ?property_shape sh:path ?property .
  {
    ?property_shape sh:class ?target_class .
  }
  UNION
  {
    ?property_shape sh:or ?alternatives .
    ?alternatives rdf:rest*/rdf:first ?alternative .
    ?alternative sh:class ?target_class .
  }
}
"""


@dataclasses.dataclass(frozen=True)
class Vocabulary:
    """Sets of property IRIs (rdflib URIRef), as described in the module docstring.

    value_set_by_class: {(class, property): True if the values are value-set individuals,
    False if they are records}, for the pairs the SHACL shapes describe.
    """

    identifier_properties: frozenset
    datatype_properties: frozenset
    value_set_properties: frozenset
    value_set_by_class: dict

    def is_value_set(self, subject_classes, property_iri):
        """Return whether `property_iri` points at value-set individuals for a subject of these classes.

        Returns None when no shape describes the property on any of the classes: the caller then
        uses the property-level rule. A subject of several classes counts as a value set only if
        the property is one on all of them.
        """
        decisions = []
        for subject_class in subject_classes:
            decision = self.value_set_by_class.get((subject_class, property_iri))
            if decision is not None:
                decisions.append(decision)
        if not decisions:
            return None
        return all(decisions)


@functools.lru_cache(maxsize=None)
def load_vocabulary(schema_path=DEFAULT_SCHEMA_PATH, shacl_path=DEFAULT_SHACL_PATH):
    """Read the schema and the SHACL shapes and return a Vocabulary (cached per pair of paths)."""
    schema_graph = Graph()
    schema_graph.parse(str(schema_path), format="turtle")
    shacl_graph = Graph()
    shacl_graph.parse(str(shacl_path), format="turtle")

    datatype_properties = set(DATATYPE_PROPERTIES_2024_2)
    identifier_properties = set()
    for row in schema_graph.query(DATATYPE_PROPERTIES_QUERY):
        datatype_properties.add(row.property)
        if "Identifier" in str(row.property):
            identifier_properties.add(row.property)

    value_set_classes = set()
    for row in schema_graph.query(VALUE_SET_CLASSES_QUERY):
        value_set_classes.add(row.value_set_class)

    # {property: set of classes its values may have, over all the shapes that use it}
    target_classes_by_property = {}
    # {(subject class, property): set of classes its values may have on that class}
    target_classes_by_pair = {}
    for row in shacl_graph.query(PROPERTY_CLASSES_QUERY):
        target_classes_by_property.setdefault(row.property, set()).add(row.target_class)
        target_classes_by_pair.setdefault((row.subject_class, row.property), set()).add(row.target_class)

    # A property is a value-set property only if every class it may point to is a value set.
    value_set_properties = set(VALUE_SET_PROPERTIES_2024_2)
    for property_iri, target_classes in target_classes_by_property.items():
        if target_classes <= value_set_classes:
            value_set_properties.add(property_iri)

    value_set_by_class = {}
    for pair, target_classes in target_classes_by_pair.items():
        value_set_by_class[pair] = target_classes <= value_set_classes

    return Vocabulary(
        identifier_properties=frozenset(identifier_properties),
        datatype_properties=frozenset(datatype_properties),
        value_set_properties=frozenset(value_set_properties),
        value_set_by_class=value_set_by_class,
    )
