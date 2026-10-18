#!/usr/bin/env bash
# Compares design variants on the development split (never on the gold set).
# Usage: evaluation/run_ablations.sh [SET]      (default: dev, items with a non-empty reference)
set -euo pipefail
cd "$(dirname "$0")/.."
SET="${1:-dev}"
run() {
  local name="$1"; shift
  echo "== $name $(date -u +%H:%M)"
  python3 -u evaluation/evaluate.py --set "$SET" --evaluable-only --out "evaluation/results/ablation_${SET}_${name}.json" "$@" \
    > "evaluation/results/ablation_${SET}_${name}.log" 2>&1
}
run base
run no_analysis --set-config analysis=false
run qwen3 --set-config model='"qwen3:8b"'
python3 evaluation/report.py evaluation/results/ablation_"${SET}"_*.json
