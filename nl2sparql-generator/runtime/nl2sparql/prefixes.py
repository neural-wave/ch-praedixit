"""Prefix handling for the SPHN knowledge graph.

The generator lets the model write prefixed names without PREFIX declarations; the
declarations are added deterministically here, so a forgotten or misspelled namespace
can never reach Virtuoso.
"""

from __future__ import annotations

import re

INFERENCE_RULE_SET = "https://biomedit.ch/rdf/sphn-resource/global"
GLOBAL_GRAPH = "https://biomedit.ch/rdf/sphn-resource/global"
PATIENT_GRAPH_PREFIX = "http://example.org/graph/"

# Namespaces used by the patient data and the terminologies loaded in the global graph.
PREFIXES: dict[str, str] = {
    "sphn": "https://biomedit.ch/rdf/sphn-schema/sphn#",
    "sphn-individual": "https://biomedit.ch/rdf/sphn-schema/sphn/individual#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "snomed": "http://snomed.info/id/",
    "icd-10-gm": "https://biomedit.ch/rdf/sphn-resource/icd-10-gm/",
    "chop": "https://biomedit.ch/rdf/sphn-resource/chop/",
    "loinc": "https://loinc.org/rdf/",
    "ucum": "https://biomedit.ch/rdf/sphn-resource/ucum/",
    "sphn-atc": "https://biomedit.ch/rdf/sphn-resource/atc/",
    "atc": "https://www.whocc.no/atc_ddd_index/?code=",
    "geno": "http://purl.obolibrary.org/obo/GENO_",
    "so": "http://purl.obolibrary.org/obo/SO_",
    "obi": "http://purl.obolibrary.org/obo/OBI_",
    "eco": "http://purl.obolibrary.org/obo/ECO_",
    "efo": "http://www.ebi.ac.uk/efo/EFO_",
    "edam": "http://edamontology.org/",
    "genepio": "http://purl.obolibrary.org/obo/GENEPIO_",
    "ordo": "http://www.orpha.net/ORDO/",
    "emdn": "https://biomedit.ch/rdf/sphn-resource/emdn/",
    "hgnc": "https://biomedit.ch/rdf/sphn-resource/hgnc/",
    "resource": "https://biomedit.ch/rdf/sphn-resource/",
    "bif": "bif:",
}

PREFIX_DECL_RE = re.compile(r"^\s*PREFIX\s+([A-Za-z][\w.-]*)?:\s*<([^>]*)>\s*$", re.IGNORECASE | re.MULTILINE)


def strip_prefix_declarations(query: str) -> tuple[str, dict[str, str]]:
    """Remove PREFIX lines, returning the body and the declared prefixes."""
    declared = {m.group(1) or "": m.group(2) for m in PREFIX_DECL_RE.finditer(query)}
    body = PREFIX_DECL_RE.sub("", query)
    return body, declared


def _strip_literals_and_iris(text: str) -> str:
    text = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
    text = re.sub(r"'(?:[^'\\]|\\.)*'", "''", text)
    text = re.sub(r"<[^<>\s]*>", "<>", text)
    return re.sub(r"#[^\n]*", "", text)


def used_prefixes(body: str) -> set[str]:
    """Prefixes referenced by prefixed names in the query body (outside strings and IRIs)."""
    clean = _strip_literals_and_iris(body)
    found = set()
    for m in re.finditer(r"(?<![\w?$:<-])([A-Za-z][\w-]*(?:\.[\w-]+)*)?:(?=[\w%(\\-]|\s|\)|$)", clean):
        name = m.group(1) or ""
        found.add(name)
    return found


def add_prefixes(query: str, extra: dict[str, str] | None = None) -> str:
    """Return the query with exactly the PREFIX declarations it needs, in a canonical order.

    Declarations already written in the query win over the default map, which keeps queries
    that bind a prefix to a non-default namespace intact.
    """
    body, declared = strip_prefix_declarations(query)
    mapping = dict(PREFIXES)
    if extra:
        mapping.update(extra)
    mapping.update(declared)
    # A bare ":hasCode" written by the model means the SPHN vocabulary.
    mapping.setdefault("", PREFIXES["sphn"])
    # DEFINE pragmas must stay before the prologue.
    defines = re.findall(r"^\s*DEFINE\s+[^\n]+$", body, flags=re.IGNORECASE | re.MULTILINE)
    body = re.sub(r"^\s*DEFINE\s+[^\n]+\n?", "", body, flags=re.IGNORECASE | re.MULTILINE)
    needed = used_prefixes(body)
    lines = [d.strip() for d in defines]
    for name in sorted(needed, key=lambda n: (n != "sphn", n)):
        if name in ("bif", "sql"):
            # Virtuoso built-in function namespaces: declaring bif: is harmless, sql: must not be declared.
            if name == "bif":
                lines.append("PREFIX bif: <bif:>")
            continue
        if name in mapping:
            lines.append(f"PREFIX {name}: <{mapping[name]}>")
        elif name == "":
            lines.append(f"PREFIX : <{PREFIXES['sphn']}>")
    return "\n".join(lines + [body.strip()]) + "\n"


def compact_iri(iri: str) -> str:
    """Shorten a full IRI to a prefixed name when possible (longest namespace wins)."""
    best = None
    for name, ns in PREFIXES.items():
        if iri.startswith(ns) and (best is None or len(ns) > len(PREFIXES[best])):
            best = name
    if best is None:
        return f"<{iri}>"
    local = iri[len(PREFIXES[best]):]
    if re.fullmatch(r"[\w][\w.\-]*[\w-]|[\w]", local) and not local.endswith("."):
        return f"{best}:{local}"
    return f"<{iri}>"
