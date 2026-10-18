"""Collect every NL/SPARQL pair shipped with the hackathon into one normalised JSONL file.

Read-only on the original folders. Output: preparation/work/all_pairs.jsonl, one record per pair:

    id, source, question, sparql, gold_section, family, variant, expected_compliant,
    expected_rule, request_kind, template_question, shorthand

Sources
  gold            hackathon-virtuoso-release/queries-example.md (15 official examples)
  evalset         syntheic data/data/evaluation_set/*.rq          (hand-written + LLM, 222)
  marzio          syntheic data/data/evaluation_marzio/generator/{train,test}.jsonl (570)
  generated       syntheic data/data/generated.jsonl              (template generator, 7374)
  testqueries     syntheic data/data/test-queries/*/*.rq          (checker unit tests, 188)

Copies of the official examples inside the other sets are tagged with ``gold_section`` so
evaluation can exclude them from any retrieval bank (no leakage).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # repository root
sys.path.insert(0, str(ROOT / "runtime"))

from nl2sparql.shorthand import expand  # noqa: E402

DEFAULT_PROJECT = ROOT.parent


def norm_ws(text: str) -> str:
    return " ".join(text.split())


def parse_gold(md_path: Path) -> list[dict]:
    text = md_path.read_text(encoding="utf-8")
    parts = re.split(r"^(\d+)\.\s+(.+)$", text, flags=re.MULTILINE)
    out = []
    for i in range(1, len(parts), 3):
        num, title, body = int(parts[i]), parts[i + 1].strip(), parts[i + 2]
        qm = re.search(r'"(.+?)"', body, flags=re.DOTALL)
        question = norm_ws(qm.group(1)) if qm else ""
        rest = body[qm.end():] if qm else body
        m = re.search(r"^\s*(PREFIX|SELECT|CONSTRUCT|ASK|DESCRIBE|DEFINE)\b", rest, flags=re.MULTILINE | re.IGNORECASE)
        sparql = rest[m.start():].strip() if m else ""
        non_compliant = "NON COMPLIANT" in title.upper()
        out.append(
            {
                "id": f"gold-{num:02d}",
                "source": "gold",
                "question": question,
                "sparql": sparql + "\n",
                "title": re.sub(r"\s*-\s*NON COMPLIANT\s*$", "", title, flags=re.IGNORECASE),
                "gold_section": num,
                "expected_compliant": (not non_compliant) if num <= 14 else False,
            }
        )
    return out


def parse_rq(path: Path) -> tuple[dict, str]:
    header, body = {}, []
    in_header = True
    for line in path.read_text(encoding="utf-8").splitlines():
        if in_header and line.startswith("#"):
            m = re.match(r"#\s*([a-z_]+):\s*(.*)$", line)
            if m:
                header[m.group(1)] = m.group(2).strip()
            continue
        in_header = False
        body.append(line)
    return header, "\n".join(body).strip() + "\n"


def gold_section_of(origin: str | None) -> int | None:
    if not origin:
        return None
    m = re.search(r"queries-example\.md section (\d+)", origin)
    return int(m.group(1)) if m else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=str(DEFAULT_PROJECT))
    ap.add_argument("--out", default=str(HERE / "work" / "all_pairs.jsonl"))
    args = ap.parse_args()
    project = Path(args.project)
    release = project / "hackathon-virtuoso-release"
    synth = project / "syntheic data" / "data"
    records: list[dict] = []

    gold = parse_gold(release / "queries-example.md")
    records += gold
    gold_by_question = {norm_ws(g["question"]).lower(): g["gold_section"] for g in gold}

    for path in sorted((synth / "evaluation_set").glob("*.rq")):
        h, q = parse_rq(path)
        records.append(
            {
                "id": f"evalset-{path.stem}",
                "source": "evalset",
                "question": h.get("question", ""),
                "sparql": q,
                "gold_section": gold_section_of(h.get("origin")),
                "expected_compliant": {"true": True, "false": False}.get(h.get("expected_compliant", ""), "ambiguous"),
                "expected_rule": h.get("expected_rules"),
                "origin": h.get("origin"),
                "notes": h.get("notes"),
                "other_correct_translations": h.get("other_correct_translations"),
            }
        )

    for split_file in ("train", "test"):
        for line in open(synth / "evaluation_marzio" / "generator" / f"{split_file}.jsonl", encoding="utf-8"):
            r = json.loads(line)
            gs = gold_by_question.get(norm_ws(r["request"]).lower()) if r.get("family") == "official" else None
            records.append(
                {
                    "id": f"marzio-{r['split']}-{r['id']}",
                    "source": "marzio",
                    "split": r["split"],
                    "question": r["request"],
                    "template_question": r.get("request_template_version"),
                    "sparql": r["sparql"].strip() + "\n",
                    "shorthand": r.get("notes"),
                    "family": r.get("family"),
                    "variant": r.get("variant"),
                    "request_source": r.get("request_source"),
                    "expected_compliant": r.get("expected_compliant"),
                    "expected_rule": r.get("expected_rule"),
                    "origin": r.get("origin"),
                    "source_id": r.get("source_id"),
                    "gold_section": gs,
                }
            )

    for line in open(synth / "generated.jsonl", encoding="utf-8"):
        r = json.loads(line)
        records.append(
            {
                "id": f"generated-{r['id']}",
                "source": "generated",
                "split": r["split"],
                "question": r["request"],
                "template_question": r.get("request_template_version"),
                "sparql": r["sparql"].strip() + "\n",
                "family": r.get("family"),
                "concept": r.get("concept"),
                "variant": r.get("variant"),
                "request_kind": r.get("request_kind"),
                "request_source": r.get("request_source"),
                "expected_compliant": r.get("expected_compliant"),
                "expected_rule": r.get("expected_rule"),
                "gold_section": None,
            }
        )

    for sub in ("examples", "rules", "adversarial", "risk-probes"):
        for path in sorted((synth / "test-queries" / sub).glob("*.rq")):
            h, q = parse_rq(path)
            gs = None
            m = re.search(r"example-(\d+)", h.get("source", ""))
            if sub == "examples" and m and gold_by_question.get(norm_ws(h.get("question", "")).lower()):
                gs = gold_by_question[norm_ws(h.get("question", "")).lower()]
            records.append(
                {
                    "id": f"testqueries-{sub}-{path.stem}",
                    "source": "testqueries",
                    "subset": sub,
                    "question": h.get("question", ""),
                    "sparql": q,
                    "description": h.get("description"),
                    "expected_compliant": {"true": True, "false": False}.get(h.get("expected_compliant", ""), "ambiguous"),
                    "expected_rule": h.get("expected_rules"),
                    "gold_section": gs,
                }
            )

    # Mark exact copies of gold queries/questions anywhere (by normalised question text).
    for r in records:
        if r["source"] != "gold" and r.get("gold_section") is None:
            gs = gold_by_question.get(norm_ws(r["question"]).lower())
            if gs is not None:
                r["gold_section"] = gs

    with open(args.out, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    by_source: dict[str, int] = {}
    for r in records:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    n_gold_copies = sum(1 for r in records if r["source"] != "gold" and r.get("gold_section"))
    print(f"wrote {len(records)} records to {args.out}: {by_source}; gold copies elsewhere: {n_gold_copies}")
    _ = expand  # imported to fail early if the package is broken


if __name__ == "__main__":
    main()
