"""Export the validated example bank as chat-format JSONL for optional LoRA fine-tuning.

Not used by the shipped pipeline (see README, "Why no fine-tuning"). It exists so that a team
with a GPU or MLX can train an adapter on exactly the supervision the retrieval bank uses:
validated, normalised pairs with the compact query as target. Official examples (and their
copies) are left out so the gold set stays untouched for evaluation.

    python3 preparation/export_finetune_data.py   # -> preparation/work/finetune_{train,valid}.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # repository root
RUNTIME = ROOT / "runtime"
WORK = ROOT / "preparation" / "work"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(ROOT))

from nl2sparql.prompts import SYSTEM_PROMPT  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", default=str(RUNTIME / "resources" / "examples.jsonl"))
    ap.add_argument("--out", default=str(WORK / "finetune_train.jsonl"))
    ap.add_argument("--valid-fraction", type=float, default=0.05)
    args = ap.parse_args()
    rows = [json.loads(line) for line in open(args.bank, encoding="utf-8") if line.strip()]
    rows = [r for r in rows if not r.get("gold_section")]
    random.Random(7).shuffle(rows)
    n_valid = max(1, int(len(rows) * args.valid_fraction))
    out = Path(args.out)
    for name, part in (("train", rows[n_valid:]), ("valid", rows[:n_valid])):
        path = out if name == "train" else out.with_name(out.stem.replace("train", "valid") + out.suffix)
        with open(path, "w", encoding="utf-8") as f:
            for r in part:
                f.write(json.dumps({"messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"QUESTION: {r['question']}"},
                    {"role": "assistant", "content": f"```sparql\n{r['compact']}\n```"},
                ]}, ensure_ascii=False) + "\n")
        print(f"{name}: {len(part)} -> {path}")


if __name__ == "__main__":
    main()
