# Compliance test set

Natural language questions paired with SPARQL queries over the SPHN patient graphs, each
labelled with the expected compliance verdict. It is used to measure the compliance checker
(both wrongly accepted and wrongly rejected queries) and to train or calibrate the models.

The rules are those of `hackathon-virtuoso-release/Query_Compliance.md` and the read-only
requirement of `hackathon-virtuoso-release/API_specification.md`.

## File layout

One `.rq` file per entry. The file is a runnable query (`./query.sh <file>`), preceded by a
header of `#` comment lines:

```sparql
# question: How many patients have type 2 diabetes?
# expected_compliant: true
# expected_rules: none
# other_correct_translations: compliant-13-type-2-diabetes-diagnosis-code-string.rq
# origin: test set
# notes: free text
PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>
SELECT ...
```

| Field | Values |
| --- | --- |
| `question` | The natural language request the query translates. |
| `expected_compliant` | `true`, `false`, or `ambiguous` when the verdict depends on an open question to the organisers (see the entry's `notes`). |
| `expected_rules` | Comma-separated rule names, main rule first; `none` for compliant entries. |
| `other_correct_translations` | Optional. Other entries that translate the same question correctly but differently, e.g. a diagnosis matched through its ICD-10-GM class or through the code string stored on a `sphn:Code` node. Their results can differ. |
| `origin` | `test set` (written for this set), `queries-example.md section N`, `bypass` (written to get around a structural check), `adversarial` (generated to get around a structural check), or `external LLM (entry-NNN.rq, rules …)` (written by an external LLM from `data/external_generation_kit/`; the original rule codes are kept, `QC1`…`QC5` being the bullet points of `Query_Compliance.md` in order). |
| `notes` | Optional. Why the entry is labelled as it is, or which checker rule it is meant to exercise. |

File names start with the expected verdict: `compliant-`, `non-compliant-`, `ambiguous-`.
Entries from the adversarial and external LLM sets carry `adversarial-` or `external-` after it.

| Origin | Entries |
| --- | --- |
| test set, queries-example.md, bypass | 94 |
| adversarial | 50 |
| external LLM | 50 |

## Rule names

The names match the `Rule` enum of `COMPLIANCE_CHECKER_PLAN.md`.

| Rule | Meaning |
| --- | --- |
| `none` | Compliant. |
| `not_read_only` | The request modifies data (`INSERT`, `DELETE`, `LOAD`, `CLEAR`, `DROP`...) or sends data out of the database (`SERVICE`, server-side functions). |
| `not_select` | `ASK`, `CONSTRUCT` or `DESCRIBE`. |
| `not_aggregated` | Returns rows about single records or patients instead of aggregates over all patients. |
| `exposes_identifier` | Returns a patient graph IRI, a record IRI, or an identifier literal (`sphn:hasIdentifier`...). |
| `single_patient` | Restricts the query to one identifiable patient. |
| `unrestricted_match` | Matches the whole graph without restricting classes or predicates. |
| `invalid_sparql` | Not valid SPARQL for Virtuoso. |

## Schema version

The hackathon brief prescribes the SPHN schema 2026.1, which is the version of
`hackathon-virtuoso-release/ontologies/sphn_rdf_schema.ttl` and of the SHACL shapes in
`hackathon-virtuoso-release/shacl/`. The mock patient files instead declare
`dct:conformsTo <https://biomedit.ch/rdf/sphn-schema/sphn/2024/2>` and use 18 terms that
2026.1 does not define. The ones the test set touches:

| Mock data (2024.2) | Schema 2026.1 |
| --- | --- |
| `sphn:hasAdmissionDateTime` on `sphn:AdministrativeCase` | `sphn:hasAdmission` → `sphn:Admission` → `sphn:hasDateTime` |
| `sphn:hasDischargeDateTime` on `sphn:AdministrativeCase` | `sphn:hasDischarge` → `sphn:Discharge` → `sphn:hasDateTime` |
| `sphn:hasRank` with a named individual (`sphn-individual:Principal`) | `sphn:hasRankCode` with a SNOMED CT code |
| `sphn:hasGeneticVariation` | not defined |

Entries written for this set follow 2026.1, so the ones that touch these concepts return no
rows on the mock data. Entries copied from `queries-example.md` are kept as the organisers
wrote them and use 2024.2 terms (`compliant-03`, `compliant-06`, `compliant-07`,
`compliant-08`, `non-compliant-02`). The schema version does not change the compliance
verdict of an entry.

## Data facts the queries rely on

- Each patient is one named graph `http://example.org/graph/<file name>`; terminologies are
  in `https://biomedit.ch/rdf/sphn-resource/global`.
- ICD-10-GM codes typed on code nodes are mostly versioned (`icd-10-gm/2023/A00.9`) and are
  reached from the unversioned code (`icd-10-gm:A00`) only through RDFS inference
  (`OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global")`).
- Besides cholera (`A00` family), 27 common ICD-10-GM codes appear: typed on code nodes of
  `sphn:BilledDiagnosis` and `sphn:OncologyDiagnosis`, and as strings
  (`sphn:hasIdentifier "E11.90"`, `sphn:hasCodingSystemAndVersion "ICD-10-GM 2024"`) on the
  `sphn:Code` nodes of `sphn:Diagnosis`.
- CHOP codes cover only the `00.0` family (therapeutic ultrasound).
- Every administrative case is in 2007. Units of `sphn:Age` vary (years, months, days...),
  so age queries filter on the unit `ucum:a`.
- `sphn:hasYear` of birth dates is mostly not a real year (e.g. `"-1741-03:31"^^xsd:gYear`),
  so queries on birth year are meaningful for compliance only, not for their results.
