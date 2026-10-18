# NL-to-SPARQL generator (SPHN / Virtuoso)

Three folders, three jobs. Only `runtime/` is needed to answer questions.

| folder | job | when you use it |
|---|---|---|
| `runtime/` | **the product**: question text in, SPARQL text out | always; this is what you ship |
| `preparation/` | builds the static data in `runtime/resources/` (once) | only to rebuild resources or export fine-tuning data |
| `evaluation/` | measures variants against reference queries | optional, development only |

## runtime/ — the shipped component

```
runtime/
  config.json       model, model server, optional SPARQL endpoint
  nl2sparql/        generator code
  resources/        static data built by preparation/: schema.json, terminology.sqlite, examples.jsonl
```

The service of this repository uses the generator through `pppp/src/pipeline.py`:
`Generator.generate(question)` writes the query, and `Generator.revise(previous)` writes it again
when the compliance checker (Gate B) rejects it, reporting to the model the blocking problems the
generator's own validation found.

What happens per question (all inside `nl2sparql/generator.py`):
1. the model lists the relevant record classes and clinical concepts (short JSON);
2. without the model: the matching part of the schema, candidate terminology codes and the 6 most
   similar validated example queries are looked up in `resources/`;
3. the model writes the query in a compact form (no PREFIX lines, `?c isa icd-10-gm:E11`);
4. code turns it into Virtuoso SPARQL (prefixes, inference options, dialect fixes);
5. checks run (schema, codes, common semantic mistakes and, if the store is reachable, execution);
   on a problem the model gets up to `max_repairs` repair rounds (0 in the shipped `config.json`:
   the service retries through `Generator.revise` instead).

Run it: the generator runs inside the service (see the repository README: `setup.sh`, `start.sh`).
To translate one question through the whole pipeline from the repository root:

```bash
venv/bin/python -m pppp.pipeline "How many patients have a billed diagnosis of cholera, by sex?"
```

If the Virtuoso store is not running (`"use_endpoint"` in `config.json`), the query is still
generated and checked statically.

### Changing the model (e.g. on the RTX 5080)

Edit `runtime/config.json`; nothing else changes.

- Ollama (detects CUDA, Metal or CPU by itself): `"backend": "ollama"`, `"model": "qwen2.5-coder:14b"`.
  A 14B model at Q4 (~9 GB) fits in 16 GB of VRAM; a 32B model (~20 GB) does not and would spill
  into system RAM.
- A fine-tuned or other model behind an OpenAI-compatible server (vLLM, llama.cpp `llama-server`,
  LM Studio): `"backend": "openai"`, `"llm_url": "http://localhost:8000"`, `"model": "<served name>"`.

## preparation/ — static data (run once, needs Docker and the hackathon folders)

| step | script | output |
|---|---|---|
| 1 | `setup_virtuoso.sh [N_PATIENTS]` | Virtuoso 7.2.16 with ontologies, RDFS rule set, a patient sample |
| 2 | `build_datasets.py` | `work/all_pairs.jsonl` (every NL/SPARQL pair of the hackathon material) |
| 3 | `build_schema.py` | `runtime/resources/schema.json` (classes and predicates found in the data) |
| 4 | `build_terminology.py` | `runtime/resources/terminology.sqlite` (code label index) |
| 5 | `build_examples.py` | `runtime/resources/examples.jsonl` (pairs validated by execution) |
| - | `export_finetune_data.py` | `work/finetune_{train,valid}.jsonl` (chat format, for a LoRA run) |

`work/` holds intermediate files: the pair collection, the validation report, an execution cache
(re-running step 5 is fast) and the patient sample list. The fine-tuning export is already done:
2,992 train and 157 validation conversations, official examples excluded.

## evaluation/ — optional

`evaluate.py --set gold|dev|test|marzio-test` runs the generator on reference questions and
compares query results on the store; `report.py` prints a table; `test_deterministic.py` holds
unit tests of the code-only parts. `results/` keeps the measured runs (dev comparisons and the
gold runs).
