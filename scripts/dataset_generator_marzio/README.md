# Data factory

Builds the training table of DATA_FACTORY.md on your Mac: requests a researcher might type, the SPARQL for each,
and a compliance label. Everything runs locally (Virtuoso in Docker, models in Ollama), so the restricted
terminology labels never leave the machine.

Run everything on the 64 GB Mac: the models need ~36 GB together. It needs `challenge/hackathon-virtuoso-release`
and `challenge/generation_folder`. If the patient files are only on another machine, run `s0_subset.py` there and copy
`generation_folder` (now holding the 2,000 subset files) plus `hackathon-virtuoso-release` without `patients/`.

## How to run

Once: install Ollama and start Docker Desktop, then quit the Ollama menu-bar app so `ollama serve` can use its port.

Terminal window 1 is the model server. Start it first and leave it open: `ollama pull` and every step need it.

```bash
OLLAMA_NUM_PARALLEL=4 ollama serve
```

Terminal window 2:

```bash
cd ~/Desktop/challenge/generation_folder
python3 -m venv .venv && source .venv/bin/activate && pip install pyyaml requests numpy   # first time only
ollama pull qwen3.6:35b-a3b && ollama pull gpt-oss:20b && ollama pull nomic-embed-text      # first time only
python3 s0_subset.py      # then run the 4 lines it prints: they load the 2,000 patients into Virtuoso on port 8891
python3 s1_profile.py     # examples 1-10 should say "shorthand result matches: True"
python3 s2_recipes.py     # both anchor lines should say OK
python3 s3_freeform.py
python3 s4_wording.py     # stop here: read ~20 questions in data/rows_allowed.jsonl before going on
python3 s5_twins.py
python3 s6_hard.py
python3 s7_finalize.py    # fill the verdict column of data/review.csv, then run it once more
python3 s8_evaluation_sets.py   # writes ../evaluation_marzio: generator/ and compliance_checker/
```

In a new window later, start with `cd ~/Desktop/challenge/generation_folder && source .venv/bin/activate`.

| Step               | What it does                                                                                                                       | Writes                                      |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------- |
| `s0_subset.py`   | picks 2,000 patients by file size and hard-links them                                                                              | `data/subset_S/`                          |
| `s1_profile.py`  | profiles S, translates ICD/CHOP labels, checks the 15 official examples                                                            | `data/profile.json`, `data/seeds.jsonl` |
| `s2_recipes.py`  | 14 of the 20 recipes filled from the profile, each query run on S (R10, R11, R14-R17 need hand-made shapes; free-form covers them) | `data/bases.jsonl`                        |
| `s3_freeform.py` | teacher writes new question+query pairs, kept only if the judge's own query gives the same result                                  | `data/bases_freeform.jsonl`               |
| `s4_wording.py`  | 3 wordings per base, judged                                                                                                        | `data/rows_allowed.jsonl`                 |
| `s5_twins.py`    | one forbidden twin per base + whole-graph family; rules must agree                                                                 | `data/rows_forbidden.jsonl`               |
| `s6_hard.py`     | hard cases (polite lists, injections, "which" about codes…)                                                                       | `data/rows_hard.jsonl`                    |
| `s7_finalize.py` | dedup, review sheet, splits                                                                                                        | `data/review.csv`, `data/dataset_v1/`   |
| `s8_evaluation_sets.py` | train (+dev) and test rows with `expected_rule`, one `.rq` per test row; notes hold the shorthand (generator) or the expanded query (compliance checker) | `../evaluation_marzio/` |

`s4` and `s5` resume where they stopped; `s3` and `s6` add more each time they run. Rejected rows and the reason
go to `data/rejected.jsonl`. In `data/review.csv` write OK, wrong label, or does not match in the `verdict` column.

The defaults are the pilot size: about 170 bases, 1,000 rows and roughly 900 model calls. For the full ~24k rows, raise
`fills_per_recipe`, `freeform_calls` and `hard_per_category` as noted in `config.yaml`.

## Output

`data/dataset_v1/` holds JSON Lines files, one row per request:
`train.jsonl`, `dev.jsonl`, `test_t1.jsonl` (official examples), `test_t2.jsonl` (held-out concepts),
`test_t3.jsonl` (worded by the other model), `test_t4.jsonl` (only if you add `data/t4_handwritten.jsonl`).

```json
{"id": "R01-0007-w1", "base_id": "R01-0007", "recipe": "R01_count_coded",
 "meaning": "Counts distinct patients with at least one billed diagnosis coded as ...",
 "request": "How many patients were billed for lymphoid leukaemia?",
 "query": "SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?r a sphn:BilledDiagnosis ; sphn:hasCode ?r_c . ?r_c isa icd-10-gm:C91 . } }",
 "label": "aggregate", "compliant": true, "rules_label": "aggregate", "result_on_S": [["37"]], "split": "train"}
```

`query` is shorthand: no PREFIX lines, and `?c isa X` means `?c rdf:type X OPTION (inference "…/global")`.
`common.expand()` turns it into a runnable query. `label` is one of `aggregate`, `single_patient`,
`individual_rows`, `identifiers`, `whole_graph`, `non_select`.
