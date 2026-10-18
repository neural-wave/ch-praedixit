#!/usr/bin/env bash
# Start the NL-to-SPARQL service: POST /nl-to-sparql on http://HOST:PORT. Run ./setup.sh once before.
#
# Starts the Ollama server if it is not running, then the API (expose.py), which loads the models
# and translates a warm-up request before accepting requests.
#
# Environment variables (all optional):
#   HOST   address to listen on (default: 0.0.0.0, every network interface)
#   PORT   port to listen on (default: 8000)

set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPOSITORY_ROOT"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
VENV_PYTHON="$REPOSITORY_ROOT/venv/bin/python"
GENERATOR_CONFIG="$REPOSITORY_ROOT/nl2sparql-generator/runtime/config.json"
LOG_DIRECTORY="$REPOSITORY_ROOT/logs"

fail() {
    echo "ERROR: $1" >&2
    exit 1
}

if [ ! -x "$VENV_PYTHON" ]; then
    fail "venv/ not found: run ./setup.sh first."
fi
if ! command -v ollama > /dev/null; then
    fail "Ollama is not installed: run ./setup.sh first."
fi

OLLAMA_MODEL="$("$VENV_PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1]))["model"])' "$GENERATOR_CONFIG")"
OLLAMA_URL="$("$VENV_PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1]))["llm_url"].rstrip("/"))' "$GENERATOR_CONFIG")"

if ! curl -sf "$OLLAMA_URL/api/tags" > /dev/null; then
    echo "Starting the Ollama server (log: logs/ollama.log)..."
    mkdir -p "$LOG_DIRECTORY"
    nohup ollama serve > "$LOG_DIRECTORY/ollama.log" 2>&1 &
    for attempt in $(seq 1 30); do
        if curl -sf "$OLLAMA_URL/api/tags" > /dev/null; then
            break
        fi
        sleep 1
    done
    curl -sf "$OLLAMA_URL/api/tags" > /dev/null || fail "The Ollama server did not start; see logs/ollama.log."
fi

if ! ollama show "$OLLAMA_MODEL" > /dev/null 2>&1; then
    fail "The model $OLLAMA_MODEL is not in Ollama: run ./setup.sh first."
fi

echo "Starting the API on http://$HOST:$PORT/nl-to-sparql (model $OLLAMA_MODEL)..."
exec "$VENV_PYTHON" expose.py --host "$HOST" --port "$PORT"
