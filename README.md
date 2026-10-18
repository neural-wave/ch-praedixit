# Secure NL-to-SPARQL over SPHN clinical knowledge graphs

A REST service that translates a natural-language request into a SPARQL query over SPHN data and
says whether the query complies with the security rules. It exposes one endpoint,
`POST /nl-to-sparql`, as described in the hackathon `API_specification.md`.

Each request goes through three steps:

1. **Gate A** (`pppp/src/checker/GateA`): a ModernBERT classifier reads the request text and
   estimates whether it asks for something the rules forbid.
2. **Generator** (`nl2sparql-generator/runtime`): an open-weight LLM served by Ollama writes the
   SPARQL query, grounded on the SPHN schema, a terminology index and validated example queries.
3. **Gate B** (`pppp/src/checker/GateB`): structural checks of the query against the rules.

When Gate B rejects the query because of a translation mistake, the generator writes it again once,
told what its own validation found wrong. The final verdict combines both gates
(`pppp/src/checker/verdict.py`).

## Requirements

- macOS or Linux, Python 3.10 or newer, `curl`;
- [Ollama](https://ollama.com/download) (it uses the GPU when there is one: Apple Metal or NVIDIA CUDA);
- disk: about 20 GB (the LLM is most of it);
- two files that are not in the repository, provided separately in
  `challenge_neuralwave_private_files.zip` ([download](https://www.swisstransfer.com/dl/01a14e24-76ad-7135-b292-0471110f3b30)):
  - the Gate A model directory (`models/gate_a/ModernBERT-base/`),
  - the terminology index `nl2sparql-generator/runtime/resources/terminology.sqlite`
    (built from the hackathon terminologies, which must not be redistributed).

## Setup (once)

1. Clone the repository and enter it:

   ```sh
   git clone https://github.com/neural-wave/ch-praedixit.git
   cd ch-praedixit
   ```

2. Download `challenge_neuralwave_private_files.zip` from
   <https://www.swisstransfer.com/dl/01a14e24-76ad-7135-b292-0471110f3b30> and extract it in
   the repository folder. The zip holds the two files under their paths in the repository, so
   they land where the service looks for them:

   ```sh
   unzip /path/to/challenge_neuralwave_private_files.zip -d .
   ```

   Contents of the zip:

   ```text
   models/gate_a/ModernBERT-base/config.json
   models/gate_a/ModernBERT-base/model.safetensors
   models/gate_a/ModernBERT-base/tokenizer.json
   models/gate_a/ModernBERT-base/tokenizer_config.json
   models/gate_a/ModernBERT-base/gate_a_settings.json
   nl2sparql-generator/runtime/resources/terminology.sqlite
   ```

3. Run the setup:

   ```sh
   ./setup.sh
   ```

`setup.sh` creates `venv/`, installs the `pppp` package, checks the two files, starts Ollama if
needed, pulls the model named in `nl2sparql-generator/runtime/config.json` and translates one test
request through the whole pipeline. It stops with a message naming the missing file if step 2 was
skipped.

Alternatively, when each file is available at a direct download link (one that returns the file
itself, not a sharing page), `setup.sh` can download them instead of step 2:

```sh
GATE_A_MODEL_URL="https://.../gate_a.zip" TERMINOLOGY_URL="https://.../terminology.sqlite" ./setup.sh
```

## Start the service

```sh
./start.sh                  # http://0.0.0.0:8000/nl-to-sparql
PORT=9000 ./start.sh        # another port
```

The service loads the models and translates a warm-up request before accepting requests.

```sh
curl -s -X POST http://127.0.0.1:8000/nl-to-sparql -H 'Content-Type: application/json' \
     -d '{"text": "How many patients have a Billed Diagnosis for Type 2 diabetes mellitus?"}'
```

Response: `{"sparql_query": "...", "tokens_consumed": 4203, "is_compliant": true}`; a non-compliant
query also has `compliance_reason`. Errors are `{"error": "..."}` (400 for a bad request, 500 when
the translation fails).

## Configuration

| File | Settings |
|---|---|
| `config/config.yaml` | Gate A model directory, generator configuration file, number of translation retries |
| `nl2sparql-generator/runtime/config.json` | LLM model and server, generator resources and options |

## Repository layout

```text
.
├── setup.sh, start.sh          # one-time setup, service start
├── expose.py                   # starts the REST API
├── config/config.yaml          # pipeline settings
├── pppp/                       # the pppp package (source in pppp/src/)
│   ├── src/api.py              # POST /nl-to-sparql
│   ├── src/pipeline.py         # Gate A -> generator -> Gate B, retries, final verdict
│   ├── src/checker/            # Gate A, Gate B, schema check, verdict (see its README.md)
│   └── tests/
├── nl2sparql-generator/        # the generator (see its README.md); runtime/ is used at inference
├── models/gate_a/              # Gate A model (not in the repository)
├── data/                       # schemas, evaluation sets
└── scripts/                    # dataset generation, training, evaluation
```

## Tests

```sh
venv/bin/pip install -e "./pppp[test]"
venv/bin/python -m pytest pppp/tests
```
