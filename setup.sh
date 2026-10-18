#!/usr/bin/env bash
# One-time setup of the NL-to-SPARQL service (needs internet). Run from anywhere: ./setup.sh
#
# What it does:
#   1. creates venv/ with Python >= 3.10 and installs the pppp package;
#   2. puts the two files that are not in the repository in place:
#      - the Gate A model directory (config/config.yaml, gate_a.model_directory),
#      - the generator terminology index (nl2sparql-generator/runtime/resources/terminology.sqlite);
#      each is downloaded from a direct link when GATE_A_MODEL_URL / TERMINOLOGY_URL is set,
#      otherwise it must already be in place;
#   3. starts Ollama if needed and pulls the model named in nl2sparql-generator/runtime/config.json;
#   4. translates one test request through the whole pipeline.
#
# Environment variables (all optional):
#   PYTHON             Python interpreter used to create venv/ (default: python3)
#   GATE_A_MODEL_URL   direct link to a .zip of the Gate A model directory
#   TERMINOLOGY_URL    direct link to terminology.sqlite
#
# A direct link returns the file itself; a sharing page (e.g. Google Drive for large files)
# returns an HTML page instead, which this script detects and rejects.

set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPOSITORY_ROOT"

PYTHON="${PYTHON:-python3}"
VENV_PYTHON="$REPOSITORY_ROOT/venv/bin/python"
GENERATOR_CONFIG="$REPOSITORY_ROOT/nl2sparql-generator/runtime/config.json"
TERMINOLOGY_PATH="$REPOSITORY_ROOT/nl2sparql-generator/runtime/resources/terminology.sqlite"
LOG_DIRECTORY="$REPOSITORY_ROOT/logs"
TEST_REQUEST="How many patients have a Billed Diagnosis for Type 2 diabetes mellitus?"

step() {
    echo
    echo "==> $1"
}

fail() {
    echo "ERROR: $1" >&2
    exit 1
}

# ---------------------------------------------------------------------------------------------
step "1. Python environment"

if ! command -v "$PYTHON" > /dev/null; then
    fail "'$PYTHON' not found. Install Python 3.10 or newer, or set PYTHON=/path/to/python3."
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    fail "Python 3.10 or newer is required; '$PYTHON' is $("$PYTHON" --version 2>&1)."
fi
if [ ! -x "$VENV_PYTHON" ]; then
    "$PYTHON" -m venv venv
fi
"$VENV_PYTHON" -m pip install --quiet --upgrade pip
"$VENV_PYTHON" -m pip install --quiet -e ./pppp
echo "Installed the pppp package in venv/ ($("$VENV_PYTHON" --version))."

# ---------------------------------------------------------------------------------------------
step "2. Files that are not in the repository"

# The Gate A model directory is the one the pipeline reads (config/config.yaml).
GATE_A_DIRECTORY="$("$VENV_PYTHON" -c 'from pppp.config import load_settings; print(load_settings().gate_a_model_directory)')"

gate_a_model_is_complete() {
    for required_file in config.json model.safetensors tokenizer.json tokenizer_config.json gate_a_settings.json; do
        if [ ! -f "$GATE_A_DIRECTORY/$required_file" ]; then
            return 1
        fi
    done
    return 0
}

if ! gate_a_model_is_complete && [ -n "${GATE_A_MODEL_URL:-}" ]; then
    echo "Downloading the Gate A model..."
    download_directory="$(mktemp -d)"
    curl -fL --progress-bar -o "$download_directory/gate_a.zip" "$GATE_A_MODEL_URL"
    # Python's zipfile module is used instead of unzip, which minimal Linux systems may lack.
    "$VENV_PYTHON" -m zipfile -e "$download_directory/gate_a.zip" "$download_directory/unzipped" \
        || fail "GATE_A_MODEL_URL did not return a .zip file (is it a direct download link?)."
    # The zip may hold the model files at its top level or inside one folder: the model
    # directory is the one that contains gate_a_settings.json.
    settings_path="$(find "$download_directory/unzipped" -name gate_a_settings.json -not -path '*/__MACOSX/*' -print -quit)"
    if [ -z "$settings_path" ]; then
        fail "The Gate A zip contains no gate_a_settings.json."
    fi
    mkdir -p "$(dirname "$GATE_A_DIRECTORY")"
    rm -rf "$GATE_A_DIRECTORY"
    mv "$(dirname "$settings_path")" "$GATE_A_DIRECTORY"
    rm -rf "$download_directory"
fi
if ! gate_a_model_is_complete; then
    fail "The Gate A model is missing or incomplete in $GATE_A_DIRECTORY.
       Put the model directory there (config.json, model.safetensors, tokenizer.json,
       tokenizer_config.json, gate_a_settings.json), or set GATE_A_MODEL_URL to a direct link to its .zip."
fi
echo "Gate A model: $GATE_A_DIRECTORY"

terminology_is_sqlite() {
    # Every SQLite database starts with this 15-byte header.
    [ -f "$TERMINOLOGY_PATH" ] && [ "$(head -c 15 "$TERMINOLOGY_PATH")" = "SQLite format 3" ]
}

if ! terminology_is_sqlite && [ -n "${TERMINOLOGY_URL:-}" ]; then
    echo "Downloading terminology.sqlite..."
    curl -fL --progress-bar -o "$TERMINOLOGY_PATH" "$TERMINOLOGY_URL"
    if ! terminology_is_sqlite; then
        rm -f "$TERMINOLOGY_PATH"
        fail "TERMINOLOGY_URL did not return an SQLite file (is it a direct download link?)."
    fi
fi
if ! terminology_is_sqlite; then
    fail "terminology.sqlite is missing in $(dirname "$TERMINOLOGY_PATH").
       Put the file there, or set TERMINOLOGY_URL to a direct link to it."
fi
echo "Terminology index: $TERMINOLOGY_PATH"

# ---------------------------------------------------------------------------------------------
step "3. Ollama and the generator model"

OLLAMA_MODEL="$("$VENV_PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1]))["model"])' "$GENERATOR_CONFIG")"
OLLAMA_URL="$("$VENV_PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1]))["llm_url"].rstrip("/"))' "$GENERATOR_CONFIG")"

if ! command -v ollama > /dev/null; then
    fail "Ollama is not installed. Install it, then run ./setup.sh again:
       macOS:  brew install ollama   (or the app from https://ollama.com/download)
       Linux:  curl -fsSL https://ollama.com/install.sh | sh"
fi

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

echo "Pulling $OLLAMA_MODEL (several GB the first time)..."
ollama pull "$OLLAMA_MODEL"

# ---------------------------------------------------------------------------------------------
step "4. Test request through the whole pipeline"

"$VENV_PYTHON" -m pppp.pipeline "$TEST_REQUEST"

echo
echo "Setup complete. Start the service with ./start.sh"
