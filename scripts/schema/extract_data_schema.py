"""Extract the schema the mock patient data actually uses, for the schema check of generated queries.

The SPHN schema 2026.1 and the mock data (schema 2024.2) do not match: the data uses 15
properties and 3 classes that 2026.1 does not define, and 80 properties of 2026.1 never occur
in it. A query is only useful if it uses what the data contains, so this script reads the
patient files and writes, to data/sphn/data_schema.json:

- classes: {class: number of records} for every sphn: class that types a record;
- properties: {property: {"kind": "record" | "literal" | "individual" | "terminology",
  "datatypes": [...], "uses": n}}: what the property's values are;
- class_properties: {class: [properties]}: the properties records of that class carry;
- covered_by: {schema class: [data classes]} for schema classes that type no record but whose
  subclasses do (a query on sphn:Diagnosis matches sphn:BilledDiagnosis records with inference).

The scan of all 100,000 mock patients found the same 966 class-property pairs as the 2,000
patients loaded in Virtuoso, so the loaded patients are enough.

Usage:
    python scripts/schema/extract_data_schema.py <folder of patient .ttl files> [--output data/sphn/data_schema.json]
"""

import argparse
import collections
import datetime
import json
import multiprocessing
import pathlib

import rdflib
from rdflib import OWL, RDF, RDFS, Literal

SPHN = "https://biomedit.ch/rdf/sphn-schema/sphn#"
INDIVIDUAL = "https://biomedit.ch/rdf/sphn-schema/sphn/individual#"
RESOURCE = "https://biomedit.ch/rdf/sphn-resource/"
TERMINOLOGY_SEGMENTS = {"atc", "chop", "dcm", "eco", "edam", "efo", "einstein", "emdn", "genepio", "geno",
                        "hgnc", "icd-10-gm", "loinc", "obi", "ordo", "so", "ucum"}
# This file is at <repository>/scripts/schema/extract_data_schema.py.
REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "data" / "sphn" / "data_schema.json"
SCHEMA_PATH = REPOSITORY_ROOT / "data" / "sphn" / "sphn_rdf_schema.ttl"


def value_kind(value):
    """Return what a property value is: a record, a literal, a value-set individual or a terminology IRI."""
    if isinstance(value, Literal):
        return "literal"
    text = str(value)
    if text.startswith(INDIVIDUAL):
        return "individual"
    if text.startswith(RESOURCE) and text[len(RESOURCE):].split("/")[0] not in TERMINOLOGY_SEGMENTS:
        return "record"
    return "terminology"


def scan_file(path):
    """Return the counts of one patient file: classes, (class, property) pairs, property kinds and datatypes."""
    graph = rdflib.Graph()
    graph.parse(path, format="turtle")
    classes_by_subject = collections.defaultdict(set)
    for subject, record_class in graph.subject_objects(RDF.type):
        if str(record_class).startswith(SPHN):
            classes_by_subject[subject].add(str(record_class)[len(SPHN):])
    classes = collections.Counter()
    for subject_classes in classes_by_subject.values():
        classes.update(subject_classes)
    pairs = collections.Counter()
    kinds = collections.Counter()
    datatypes = collections.Counter()
    for subject, predicate, value in graph:
        if predicate == RDF.type or not str(predicate).startswith(SPHN):
            continue
        property_name = str(predicate)[len(SPHN):]
        for record_class in classes_by_subject.get(subject, ()):
            pairs[(record_class, property_name)] += 1
        kind = value_kind(value)
        kinds[(property_name, kind)] += 1
        if kind == "literal" and value.datatype is not None:
            datatypes[(property_name, str(value.datatype).split("#")[-1])] += 1
    return classes, pairs, kinds, datatypes


def schema_subclasses():
    """Return {schema class: set of its sphn: subclasses at any depth} from the SPHN schema."""
    schema = rdflib.Graph()
    schema.parse(str(SCHEMA_PATH), format="turtle")
    direct = collections.defaultdict(set)
    for subclass, superclass in schema.subject_objects(RDFS.subClassOf):
        if str(subclass).startswith(SPHN) and str(superclass).startswith(SPHN):
            direct[str(superclass)[len(SPHN):]].add(str(subclass)[len(SPHN):])
    all_classes = {str(c)[len(SPHN):] for c in schema.subjects(RDF.type, OWL.Class) if str(c).startswith(SPHN)}
    closure = {}
    for schema_class in all_classes:
        found, frontier = set(), {schema_class}
        while frontier:
            frontier = {sub for parent in frontier for sub in direct.get(parent, ())} - found
            found |= frontier
        closure[schema_class] = found
    return closure


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("patients", type=pathlib.Path, help="folder of patient .ttl files")
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--processes", type=int, default=8)
    arguments = parser.parse_args()

    files = sorted(arguments.patients.glob("*.ttl"))
    classes, pairs, kinds, datatypes = (collections.Counter() for _ in range(4))
    with multiprocessing.Pool(arguments.processes) as pool:
        for file_classes, file_pairs, file_kinds, file_datatypes in pool.imap_unordered(scan_file, files, chunksize=8):
            classes.update(file_classes)
            pairs.update(file_pairs)
            kinds.update(file_kinds)
            datatypes.update(file_datatypes)

    properties = {}
    for (property_name, kind), uses in kinds.items():
        entry = properties.setdefault(property_name, {"kind": kind, "datatypes": [], "uses": 0})
        entry["uses"] += uses
        if entry["kind"] != kind:
            entry["kind"] = "mixed"
    for (property_name, datatype), _ in datatypes.items():
        properties[property_name]["datatypes"].append(datatype)
    for entry in properties.values():
        entry["datatypes"] = sorted(set(entry["datatypes"]))
    class_properties = collections.defaultdict(list)
    for record_class, property_name in sorted(pairs):
        class_properties[record_class].append(property_name)
    covered_by = {}
    for schema_class, subclasses in sorted(schema_subclasses().items()):
        if schema_class in classes:
            continue
        in_data = sorted(subclasses & set(classes))
        if in_data:
            covered_by[schema_class] = in_data

    data_schema = {
        "source": {"folder": arguments.patients.name, "files": len(files),
                   "extracted": datetime.date.today().isoformat()},
        "classes": dict(sorted(classes.items())),
        "properties": dict(sorted(properties.items())),
        "class_properties": dict(class_properties),
        "covered_by": covered_by,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(data_schema, indent=1) + "\n", encoding="utf-8")
    print(f"{len(files)} files: {len(classes)} classes, {len(properties)} properties, {len(pairs)} class-property pairs, "
          f"{len(covered_by)} schema classes covered by subclasses -> {arguments.output}")


if __name__ == "__main__":
    main()
