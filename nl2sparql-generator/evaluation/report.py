"""Summarise one or more evaluation result files as a Markdown table (with per-subset rows).

    python3 evaluation/report.py evaluation/results/*.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # repository root
RUNTIME = ROOT / "runtime"
WORK = ROOT / "preparation" / "work"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(ROOT))

from evaluation.evaluate import summarise  # noqa: E402

COLS = [
    ("n", "n"),
    ("syntax_valid", "syntax"),
    ("schema_consistent", "schema"),
    ("exact_match", "exact"),
    ("element_f1", "elem F1"),
    ("exec_strict", "exec"),
    ("exec_relaxed", "exec relaxed"),
    ("exec_numeric", "exec numeric"),
    ("exec_projection", "exec projection"),
    ("n_reference_evaluable", "n eval"),
    ("exec_strict_on_evaluable", "exec (eval)"),
    ("exec_relaxed_on_evaluable", "relaxed (eval)"),
    ("exec_numeric_on_evaluable", "numeric (eval)"),
    ("exec_projection_on_evaluable", "projection (eval)"),
    ("latency_mean_s", "latency s"),
    ("tokens_mean", "tokens"),
]


def subset(rows: list[dict], name: str) -> list[dict]:
    if name == "compliant":
        return [r for r in rows if r.get("expected_compliant") is True]
    if name == "non-compliant":
        return [r for r in rows if r.get("expected_compliant") is False]
    return rows


def fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.3f}" if v <= 1.0 else f"{v:.1f}"
    return str(v)


def main() -> None:
    print("| run | " + " | ".join(h for _, h in COLS) + " |")
    print("|" + "---|" * (len(COLS) + 1))
    for path in sys.argv[1:]:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = data["items"]
        for sub in ("all", "compliant", "non-compliant"):
            rs = subset(rows, sub) if sub != "all" else rows
            if not rs or (sub != "all" and len(rs) == len(rows)):
                continue
            s = summarise(rs)
            s["n"] = len(rs)
            name = Path(path).stem + ("" if sub == "all" else f" [{sub}]")
            print(f"| {name} | " + " | ".join(fmt(s.get(k)) for k, _ in COLS) + " |")


if __name__ == "__main__":
    main()
