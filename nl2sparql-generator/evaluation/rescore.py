"""Recompute the execution metrics of a results file without regenerating the queries.

    python3 evaluation/rescore.py evaluation/results/eval_gold.json
    python3 evaluation/rescore.py --first-attempt evaluation/results/eval_dev.json --out evaluation/results/eval_dev_norepair.json

--first-attempt scores the query of the first generation attempt (before any repair),
which measures what the validate-and-repair loop adds without a second model run.
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

from evaluation.compare import compare  # noqa: E402
from evaluation.evaluate import canonical, elements, f1, summarise  # noqa: E402
from nl2sparql.shorthand import compact, expand  # noqa: E402
from nl2sparql.sparql_client import SparqlClient  # noqa: E402


def main() -> None:
    import argparse

    from nl2sparql.schema import Schema
    from nl2sparql.terminology import Terminology
    from nl2sparql.validate import check_schema

    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--first-attempt", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    client = SparqlClient(timeout=120)
    schema = Schema(RUNTIME / "resources" / "schema.json")
    term = Terminology(RUNTIME / "resources" / "terminology.sqlite")
    for path in args.paths:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        for r in data["items"]:
            if args.first_attempt and r.get("trace", {}).get("attempts"):
                r["generated"] = r["trace"]["attempts"][0]["query"]
                r["attempts"] = 1
                errs = [x for x in check_schema(r["generated"], schema, term)
                        if x.code in ("unknown_term", "predicate_not_in_data", "predicate_domain", "unknown_code",
                                      "unknown_individual")]
                r["schema_consistent"] = not errs
            ref = client.query(expand(compact(r["reference"])), timeout=120)
            pred = client.query(r["generated"], timeout=120)
            no = {"strict": False, "relaxed": False, "iri_relaxed": False, "numeric": False, "projection": False}
            cmp = compare(ref, pred) if ref.ok and pred.ok else no
            r.update({"exec_strict": cmp["strict"], "exec_relaxed": cmp["relaxed"],
                      "exec_iri_relaxed": cmp["iri_relaxed"], "exec_numeric": cmp["numeric"],
                      "exec_projection": cmp["projection"], "syntax_valid": pred.ok, "reference_ok": ref.ok})
            r.setdefault("exec_strict_raw_reference", False)
            r["exact_match"] = canonical(r["generated"]) == canonical(r["reference"])
            r["element_f1"] = round(f1(elements(r["generated"]), elements(expand(compact(r["reference"])))), 3)
        cfg = data["summary"].get("config")
        summary = summarise(data["items"])
        summary.update({k: data["summary"].get(k) for k in ("set", "n", "bank_size")})
        summary["config"] = cfg
        data["summary"] = summary
        target = Path(args.out) if args.out else p
        target.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        print(target.name, json.dumps({k: v for k, v in summary.items() if k != "config"}))


if __name__ == "__main__":
    main()
