## Existing rules (Query_Compliance.md)

- Queries about a single, identifiable patient are not allowed.
- Queries must always return aggregated information across all patients (e.g. "give me the number of patients that ...").
- Queries that attempt to match the whole graph without restriction are not allowed.
- Only SELECT queries are allowed
- No identifiers should be exposed like patient ID, laboratory ID, diagnosis ID, etc ...

## New rules

- Queries that keep only small groups are not allowed: a `HAVING` that keeps only groups of 5 patients or fewer (e.g. "codes that only one patient has") counts as a single patient.
- Queries must not group by an exact value or date-time (e.g. "number of patients per exact BMI value"): almost every group is one record, so the result counts as not aggregated.
- Queries must not exclude a specific patient (e.g. "all patients except patient 1000042"): compared with the full count, the result reveals that patient.
- Queries must not filter on an exact timestamp or a full date (e.g. "admitted at exactly 2024-02-12 14:06:11", "born on 14 March 1975"). Date ranges, years and months are fine.
- Queries must not fix 3 or more identifying attributes to exact values (sex, birth date or country, nationality, residence, civil status, death date, exact age), e.g. "Swiss men born in 1975". Ranges and group-bys are fine.
- Queries must not return one row per patient, even without IDs (e.g. "for each patient, how many diagnoses do they have?"). Ask for one number about the whole group instead (e.g. the average per patient).
