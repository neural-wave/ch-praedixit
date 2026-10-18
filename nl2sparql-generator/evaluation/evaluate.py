"""Evaluate the generator against reference NL/SPARQL pairs.

Metrics per question (reported as rates over the set):
  syntax_valid      the generated query is accepted and executed by Virtuoso
  schema_consistent no unknown SPHN term, predicate on a class that never uses it, or unknown code
  exact_match       identical to the reference after canonicalisation (whitespace, prefixes, isa)
  element_f1        F1 over the schema elements used (classes, predicates, codes, named individuals)
  exec_strict       same result table as the reference (column names and row order ignored)
  exec_relaxed      reference columns all present with the same rows (extra columns allowed)
  exec_iri_relaxed  as relaxed, comparing IRIs by local name (code IRI vs code string)
  exec_projection   same rows, prediction columns a subset (or superset) of the reference columns
  exec_numeric      same row count and the reference's numeric columns reproduced; group labels
                    may be presented differently (snomed:248153007 vs "male")
  latency, tokens

Leakage control: the example bank used during evaluation excludes every reference item of
the evaluated set, every copy of an official example (for --set gold), and every bank entry
whose query is identical to an evaluated reference query.

Usage:
  python3 evaluation/evaluate.py --set gold --out evaluation/results/gold_eval.json
  python3 evaluation/evaluate.py --set dev  --limit 40
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # repository root
RUNTIME = ROOT / "runtime"
WORK = ROOT / "preparation" / "work"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(ROOT))

from evaluation.compare import compare  # noqa: E402
from nl2sparql.generator import Generator, load_config  # noqa: E402
from nl2sparql.prefixes import strip_prefix_declarations  # noqa: E402
from nl2sparql.shorthand import compact, expand  # noqa: E402
from nl2sparql.sparql_client import SparqlClient  # noqa: E402
from nl2sparql.validate import check_schema  # noqa: E402

SCHEMA_ERRORS = {"unknown_term", "predicate_not_in_data", "predicate_domain", "unknown_code", "unknown_individual"}


def canonical(query: str) -> str:
    body, _ = strip_prefix_declarations(expand(compact(query)))
    body = re.sub(r"#[^\n]*", "", body)
    return " ".join(body.split())


def elements(query: str) -> set[str]:
    body, _ = strip_prefix_declarations(query)
    body = re.sub(r'"[^"]*"', "", body)
    pat = r"\b(?:sphn|sphn-individual|snomed|icd-10-gm|chop|loinc|ucum|geno|so|obi|ordo):[\w.\-]*[\w]"
    return set(re.findall(pat, body))


def f1(pred: set[str], ref: set[str]) -> float:
    if not pred and not ref:
        return 1.0
    tp = len(pred & ref)
    if tp == 0:
        return 0.0
    p, r = tp / len(pred), tp / len(ref)
    return 2 * p * r / (p + r)


def split_of(item_id: str) -> str:
    """Deterministic 50/50 dev/test split of the evaluation_set entries."""
    return "dev" if int(hashlib.md5(item_id.encode()).hexdigest(), 16) % 2 == 0 else "test"


def load_set(name: str) -> list[dict]:
    pairs = [json.loads(line) for line in open(WORK / "all_pairs.jsonl", encoding="utf-8")]
    if name == "gold":
        return [p for p in pairs if p["source"] == "gold"]
    if name in ("dev", "test"):
        items = [p for p in pairs if p["source"] == "evalset" and not p.get("gold_section")
                 and "invalid_sparql" not in str(p.get("expected_rule"))]
        return [p for p in items if split_of(p["id"]) == name]
    if name == "marzio-test":
        return [p for p in pairs if p["source"] == "marzio" and p.get("split") == "test" and not p.get("gold_section")]
    raise SystemExit(f"unknown set {name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="gold", choices=["gold", "dev", "test", "marzio-test"])
    ap.add_argument("--config", default=str(RUNTIME / "config.json"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="", help="comma-separated item ids to run")
    ap.add_argument("--evaluable-only", action="store_true",
                    help="only items whose reference returns data on the store (evaluation/evaluable_ids.json)")
    ap.add_argument("--set-config", action="append", default=[], help="override key=value (JSON value)")
    args = ap.parse_args()

    items = load_set(args.set)
    if args.ids:
        wanted = set(args.ids.split(","))
        items = [i for i in items if i["id"] in wanted]
    if args.evaluable_only:
        ok = set(json.loads((ROOT / "evaluation" / "evaluable_ids.json").read_text())[args.set])
        items = [i for i in items if i["id"] in ok]
    if args.limit:
        items = items[: args.limit]
    overrides = {}
    for kv in args.set_config:
        k, v = kv.split("=", 1)
        try:
            overrides[k] = json.loads(v)
        except json.JSONDecodeError:
            overrides[k] = v
    cfg = load_config(args.config if Path(args.config).exists() else None, **overrides)

    excluded_ids = {i["id"] for i in items}
    excluded_queries = {canonical(i["sparql"]) for i in items}
    excluded_questions = {" ".join(i["question"].lower().split()) for i in items}
    excl_gold = args.set == "gold"

    def bank_filter(ex: dict) -> bool:
        if ex["id"] in excluded_ids:
            return True
        if excl_gold and ex.get("gold_section"):
            return True
        if " ".join(ex["question"].lower().split()) in excluded_questions:
            return True
        return canonical(ex["sparql"]) in excluded_queries

    t0 = time.time()
    gen = Generator(cfg, bank_filter=bank_filter)
    print(f"generator ready in {time.time() - t0:.1f}s; bank {len(gen.bank)} examples; endpoint "
          f"{'on' if gen.client else 'off'}; model {cfg['model']}", flush=True)
    client = SparqlClient(cfg["endpoint"], timeout=120)
    gen.llm.warm_up()

    rows = []
    out = Path(args.out or ROOT / "evaluation" / "results" / f"eval_{args.set}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = open(out.with_suffix(".partial.jsonl"), "w", encoding="utf-8")
    for n, item in enumerate(items, 1):
        res = gen.generate(item["question"])
        ref_q = item["sparql"]
        # Normalised reference: the round-trip-validated expansion, which adds RDFS inference to
        # unversioned ICD-10-GM/CHOP/SNOMED class constants (without it they match nothing on the
        # versioned codes stored in the data). The raw reference is compared as well.
        ref_norm = expand(compact(ref_q))
        ref = client.query(ref_norm, timeout=120)
        ref_raw = client.query(ref_q, timeout=120) if ref_norm.strip() != ref_q.strip() else ref
        pred = client.query(res.sparql, timeout=120)
        no = {"strict": False, "relaxed": False, "iri_relaxed": False, "numeric": False, "projection": False}
        cmp = compare(ref, pred) if ref.ok and pred.ok else no
        cmp_raw = compare(ref_raw, pred) if ref_raw.ok and pred.ok else no
        schema_errs = [p for p in check_schema(res.sparql, gen.schema, gen.terminology) if p.code in SCHEMA_ERRORS]
        ref_empty = ref.ok and (not ref.rows or all(all(v.get("value") in ("0", "") for v in r.values()) for r in ref.rows[:1])) \
            and ref.boolean is None
        row = {
            "id": item["id"],
            "question": item["question"],
            "expected_compliant": item.get("expected_compliant"),
            "reference": ref_q,
            "generated": res.sparql,
            "syntax_valid": pred.ok,
            "pred_error": pred.error,
            "schema_consistent": not schema_errs,
            "schema_errors": [p.message for p in schema_errs],
            "exact_match": canonical(res.sparql) == canonical(ref_q),
            "element_f1": round(f1(elements(res.sparql), elements(expand(compact(ref_q)))), 3),
            "exec_strict": cmp["strict"],
            "exec_relaxed": cmp["relaxed"],
            "exec_iri_relaxed": cmp["iri_relaxed"],
            "exec_numeric": cmp["numeric"],
            "exec_projection": cmp["projection"],
            "exec_strict_raw_reference": cmp_raw["strict"],
            "reference_ok": ref.ok,
            "reference_empty": ref_empty,
            "ref_rows": len(ref.rows),
            "pred_rows": len(pred.rows),
            "latency_s": res.seconds,
            "attempts": res.attempts,
            "problems": res.problems,
            "usage": res.usage,
            "trace": res.trace,
        }
        rows.append(row)
        partial.write(json.dumps(row, ensure_ascii=False) + "\n")
        partial.flush()
        flag = "OK " if row["exec_strict"] else ("rel" if row["exec_relaxed"] or row["exec_iri_relaxed"] else "-- ")
        print(f"[{n}/{len(items)}] {flag} syn={int(row['syntax_valid'])} sch={int(row['schema_consistent'])} "
              f"f1={row['element_f1']:.2f} {res.seconds:5.1f}s tok={res.usage.get('total_tokens')} {item['id']}",
              flush=True)

    summary = summarise(rows)
    summary.update({"set": args.set, "n": len(rows), "config": cfg, "bank_size": len(gen.bank)})
    out.write_text(json.dumps({"summary": summary, "items": rows}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "config"}, indent=1))
    print(f"wrote {out}")


def summarise(rows: list[dict]) -> dict:
    n = len(rows) or 1
    rate = lambda k, rs=rows: round(sum(1 for r in rs if r[k]) / (len(rs) or 1), 3)  # noqa: E731
    evaluable = [r for r in rows if r["reference_ok"] and not r["reference_empty"]]
    lat = [r["latency_s"] for r in rows]
    toks = [r["usage"].get("total_tokens", 0) for r in rows]
    return {
        "syntax_valid": rate("syntax_valid"),
        "schema_consistent": rate("schema_consistent"),
        "exact_match": rate("exact_match"),
        "element_f1": round(sum(r["element_f1"] for r in rows) / n, 3),
        "exec_strict": rate("exec_strict"),
        "exec_relaxed": rate("exec_relaxed"),
        "exec_iri_relaxed": rate("exec_iri_relaxed"),
        "exec_numeric": rate("exec_numeric"),
        "exec_projection": rate("exec_projection"),
        "exec_strict_raw_reference": rate("exec_strict_raw_reference"),
        "n_reference_evaluable": len(evaluable),
        "exec_strict_on_evaluable": rate("exec_strict", evaluable),
        "exec_relaxed_on_evaluable": rate("exec_relaxed", evaluable),
        "exec_iri_relaxed_on_evaluable": rate("exec_iri_relaxed", evaluable),
        "exec_numeric_on_evaluable": rate("exec_numeric", evaluable),
        "exec_projection_on_evaluable": rate("exec_projection", evaluable),
        "latency_mean_s": round(statistics.mean(lat), 2) if lat else None,
        "latency_median_s": round(statistics.median(lat), 2) if lat else None,
        "tokens_mean": round(statistics.mean(toks)) if toks else None,
        "repairs_mean": round(statistics.mean(r["attempts"] - 1 for r in rows), 2) if rows else None,
    }


if __name__ == "__main__":
    main()
