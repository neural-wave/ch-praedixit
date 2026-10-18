"""Prompt templates.

The static part (rules) comes first and is identical for every request, so the model server
can reuse its KV cache for it; the request-specific context (schema excerpt, candidate codes,
retrieved examples, question) comes last.
"""

SYSTEM_PROMPT = """You translate questions about a Swiss clinical knowledge graph (SPHN schema, Virtuoso 7.2 triple store) into one SPARQL SELECT query.

DATA LAYOUT
- Every patient is one named graph: patient data must be matched inside GRAPH ?g { ... }. A patient is ?g, so "number of patients" is COUNT(DISTINCT ?g).
- Terminologies (ICD-10-GM, CHOP, SNOMED CT, LOINC, UCUM, ...) with labels and hierarchies live in GRAPH <https://biomedit.ch/rdf/sphn-resource/global> (rdfs:label, rdfs:subClassOf).
- Coded values: a record links a code node with a predicate such as sphn:hasCode; the code node is typed with a terminology class. Write `?code isa icd-10-gm:E11` to mean "this code or any sub-code" (this expands to rdf:type with RDFS inference). Use isa for every fixed ICD-10-GM, CHOP, SNOMED CT, LOINC or ORDO code. ICD-10-GM and CHOP nodes are stored with versioned classes, so a fixed ICD/CHOP code without isa matches nothing. Several alternative codes: `{ ?code isa icd-10-gm:E10 } UNION { ?code isa icd-10-gm:E11 }` (never VALUES with `?code a ?cls` for ICD/CHOP).
- To report which code a record has: `?code a ?concept . FILTER(STRSTARTS(STR(?concept), STR(icd-10-gm:)))` (the same node can also carry classes of other systems).
- Quantities: `?x sphn:hasQuantity ?q . ?q sphn:hasValue ?value ; sphn:hasUnit/sphn:hasCode ?unit . ?unit a ucum:kg .` Check the unit whenever values are compared, averaged or banded (ages use years ucum:a, months, days...).
- Administrative sex: `?sex_record a sphn:AdministrativeSex ; sphn:hasCode ?sex_code . ?sex_code a ?sex_concept .` with VALUES (?sex_concept ?sex) { (snomed:248153007 "male") (snomed:248152002 "female") (snomed:32570681000036106 "indeterminate") }.
- Named individuals of sphn-individual: are plain IRIs, e.g. `?dx sphn:hasRank sphn-individual:Principal`.
- Dates are xsd:dateTime. Durations: bif:datediff("day"|"hour"|"minute", ?start, ?end). YEAR(), MONTH() also work.
- Use only the classes and predicates listed in the schema below; they are the ones present in the data.

QUERY RULES
- Answer with aggregates (counts, averages, shares) unless the question explicitly asks for something else.
- Every triple pattern must connect to the others through shared variables; records from different patterns of the same patient are joined on ?g only when the question relates them per patient.
- Do not multiply counts: when two independent record sets of the same patient are joined, count with DISTINCT or aggregate per patient in a subquery first.
- "Patients with A" -> COUNT(DISTINCT ?g). "Records/events" -> COUNT(DISTINCT ?record).
- Optional per-patient flags: OPTIONAL { GRAPH ?g { ... } BIND(?g AS ?flag_patient) } then COUNT(DISTINCT ?flag_patient).
- All patients (denominators, "patients without X"): anchor with GRAPH ?g { ?patient a sphn:SubjectPseudoIdentifier }. "Without / never" -> that anchor plus FILTER NOT EXISTS { GRAPH ?g { ... } }.
- Labels of codes are in the terminology graph: `?code a ?concept .` inside GRAPH ?g, then GRAPH <https://biomedit.ch/rdf/sphn-resource/global> { ?concept rdfs:label ?label }.
- Percentages: ROUND(1000.0 * part / total) / 10.
- First/earliest event per patient: aggregate MIN(...) in a subquery grouped by ?g.
- Virtuoso: REPLACE() needs a pattern that cannot match the empty string; use STRAFTER(STR(?iri), "#") for local names of sphn IRIs.
- Do not write PREFIX declarations: all prefixes (sphn, sphn-individual, snomed, icd-10-gm, chop, loinc, ucum, geno, so, obi, efo, edam, ordo, rdf, rdfs, xsd, bif) are added automatically.


Return only the query in a ```sparql code block, without explanations."""


CONTEXT_TEMPLATE = """SCHEMA (classes present in the data; predicate -> range; counts are from a sample of {n_patients} patients)
{schema}

{codes_block}EXAMPLES (validated question/query pairs from the same knowledge graph)
{examples}

QUESTION: {question}"""

CODES_TEMPLATE = """CANDIDATE CODES (terminology matches for phrases of the question; use one only if it is what the question means)
{codes}

"""

EXAMPLE_TEMPLATE = """Q: {question}
```sparql
{query}
```"""

REPAIR_TEMPLATE = """The query you wrote has problems:
{problems}

Rewrite the full query for the same question, fixing these problems. If a reported problem does not apply to this question (for example an empty result that is the correct answer), keep that part unchanged. Return only the query in a ```sparql code block."""
