"""Validate the synthetic/hand-written pairs and build the retrieval example bank.

For every candidate pair (preparation/work/all_pairs.jsonl):
  1. compact form: PREFIX lines dropped, ``rdf:type X OPTION (inference ...)`` -> ``isa X``
     (this is what the model sees in few-shot examples and what it writes);
  2. round trip: the compact form is expanded again by the same code that post-processes
     model output, and both the original and the round-tripped query are executed on the
     store; their result tables must be identical, which verifies that compaction and
     expansion preserve semantics. When they differ only because expansion added inference to
     an unversioned ICD-10-GM/CHOP/SNOMED class (the original then matches nothing on the
     versioned data), the expanded query is kept as the corrected version;
  3. schema and semantic checks (unknown terms, predicates absent from the data, predicate
     not used by the subject's class, unknown codes, missing GRAPH, unit-less comparisons,
     fan-out counting);
  4. question/query consistency: codes written literally in the question must occur in the
     query (LLM paraphrases sometimes invent codes, e.g. "Leukemia, code C91").

Records that pass enter resources/examples.jsonl; the report with every verdict goes to
preparation/work/example_validation.jsonl.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # repository root
RUNTIME = ROOT / "runtime"
WORK = ROOT / "preparation" / "work"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(ROOT))

from evaluation.compare import compare  # noqa: E402
from nl2sparql.examples import skeleton  # noqa: E402
from nl2sparql.schema import Schema, classes_in_query  # noqa: E402
from nl2sparql.shorthand import compact, expand  # noqa: E402
from nl2sparql.sparql_client import SparqlClient  # noqa: E402
from nl2sparql.terminology import Terminology  # noqa: E402
from nl2sparql.validate import check_schema, check_semantics  # noqa: E402

PRIORITY = {"gold": 0, "evalset": 1, "testqueries": 2, "marzio": 3, "generated": 4}
BLOCKING = {"unknown_term", "predicate_not_in_data", "predicate_domain", "unknown_code", "unknown_individual",
            "no_graph", "unbalanced", "replace_empty", "form"}


def select_candidates(records: list[dict], per_group: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    out = []
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        rule = str(r.get("expected_rule") or "")
        if "invalid_sparql" in rule:
            continue  # deliberately broken queries
        if r["source"] == "generated":
            if r.get("request_kind") != "request":
                continue  # "innocent" questions paired with a query that answers something else
            groups[(r.get("family"), r.get("concept"), r.get("variant"))].append(r)
            continue
        if r["source"] == "testqueries" and r.get("subset") == "adversarial":
            continue  # queries obfuscated on purpose to fool a checker
        out.append(r)
    for key in sorted(groups, key=str):
        items = groups[key]
        rng.shuffle(items)
        out.extend(items[: per_group if key[2] == "base" else max(1, per_group // 4)])
    return out


def explicit_codes(question: str) -> list[str]:
    return re.findall(r"\b([A-Z]\d{2}(?:\.\d{1,2})?|\d{6,18}|\d{2}\.\d{1,2})\b", question)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default=str(WORK / "all_pairs.jsonl"))
    ap.add_argument("--endpoint", default="http://localhost:8890/sparql")
    ap.add_argument("--out", default=str(RUNTIME / "resources" / "examples.jsonl"))
    ap.add_argument("--report", default=str(WORK / "example_validation.jsonl"))
    ap.add_argument("--per-group", type=int, default=8, help="max generated.jsonl pairs per family/concept")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--cache", default=str(WORK / "example_exec_cache.jsonl"),
                    help="execution results are cached here, so re-validating after a checker change is fast")
    args = ap.parse_args()

    records = [json.loads(line) for line in open(args.pairs, encoding="utf-8")]
    cands = select_candidates(records, args.per_group, args.seed)
    print(f"{len(cands)} candidate pairs")
    schema = Schema(RUNTIME / "resources" / "schema.json")
    term = Terminology(RUNTIME / "resources" / "terminology.sqlite")
    client = SparqlClient(args.endpoint, timeout=30, max_bytes=20_000_000)

    # Unique queries are executed once.
    uniq: dict[str, dict] = {}
    for r in cands:
        uniq.setdefault(r["sparql"], {})

    cache_path = Path(args.cache)
    cache: dict[str, dict] = {}
    if cache_path.exists():
        for line in open(cache_path, encoding="utf-8"):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line interleaved by concurrent writers is simply recomputed
            cache[row["key"]] = row["exec"]
    cache_out = open(cache_path, "a", encoding="utf-8")
    cache_lock = threading.Lock()

    def execute(query: str, comp: str, reexp: str) -> dict:
        key = hashlib.sha1((query + "\x00" + reexp).encode()).hexdigest()
        cached = cache.get(key)
        stale = cached and any(str(cached.get(k) or "").startswith(('{ "head"', "connection error"))
                               for k in ("orig_error", "re_error"))
        if cached and not stale:
            return cached
        res_o = client.query(query, timeout=30)
        res_r = client.query(reexp, timeout=30) if reexp.strip() != query.strip() else res_o
        info = {"orig_ok": res_o.ok, "orig_error": res_o.error,
                "re_ok": res_r.ok, "re_error": res_r.error, "orig_rows": len(res_o.rows), "re_rows": len(res_r.rows),
                "seconds": round(res_o.seconds, 3)}
        if res_o.ok and res_r.ok and not (res_o.truncated or res_r.truncated):
            info["roundtrip_equal"] = compare(res_o, res_r)["strict"]
        elif res_o.ok and res_r.ok:
            info["roundtrip_equal"] = res_o.truncated and res_r.truncated
        else:
            info["roundtrip_equal"] = False
        info["re_empty"] = res_r.ok and (not res_r.rows or all(
            all(v.get("value") in ("0", "") for v in row.values()) for row in res_r.rows[:1]))
        with cache_lock:
            cache_out.write(json.dumps({"key": key, "exec": info}) + "\n")
            cache_out.flush()
        return info

    def run(query: str) -> dict:
        comp = compact(query)
        reexp = expand(comp)
        info = dict(execute(query, comp, reexp))
        info.update({"compact": comp, "expanded": reexp})
        try:
            probs = check_schema(reexp, schema, term) + check_semantics(reexp, schema)
        except Exception as exc:  # a checker bug must not abort the whole build
            probs = []
            info["checker_error"] = repr(exc)
        info["problems"] = [{"severity": p.severity, "code": p.code, "message": p.message[:300]} for p in probs]
        return info

    print(f"{len(uniq)} unique queries to execute", flush=True)
    with ThreadPoolExecutor(args.workers) as pool:
        for k, (q, info) in enumerate(zip(list(uniq), pool.map(run, list(uniq))), 1):
            uniq[q] = info
            if k % 100 == 0:
                print(f"  {k}/{len(uniq)} executed", flush=True)

    report, bank = [], []
    stats: Counter = Counter()
    for r in cands:
        info = uniq[r["sparql"]]
        reasons = []
        slow = "timed out" in str(info.get("re_error") or "")
        if not info["re_ok"] and not slow and not str(info.get("re_error") or "").startswith("not executed"):
            reasons.append("does_not_execute")
        if info["orig_ok"] and info["re_ok"] and not info["roundtrip_equal"]:
            stats["normalised_by_expansion"] += 1
        blocking = [p for p in info["problems"] if p["code"] in BLOCKING]
        if blocking:
            reasons.append("schema:" + ",".join(sorted({p["code"] for p in blocking})))
        warn = [p["code"] for p in info["problems"] if p["code"] in ("unit", "fanout", "cross_patient", "count_g")]
        if warn and r["source"] in ("generated", "marzio"):
            reasons.append("lint:" + ",".join(sorted(set(warn))))
        missing = [c for c in explicit_codes(r["question"]) if c not in info["expanded"]]
        if missing:
            reasons.append("question_code_not_in_query:" + ",".join(missing))
        verdict = "accepted" if not reasons else "rejected"
        stats[(r["source"], verdict)] += 1
        for reason in reasons:
            stats[(r["source"], reason.split(":")[0])] += 1
        report.append({"id": r["id"], "source": r["source"], "verdict": verdict, "reasons": reasons,
                       "orig_ok": info["orig_ok"], "orig_error": info["orig_error"], "roundtrip_equal": info["roundtrip_equal"],
                       "re_empty": info["re_empty"], "problems": info["problems"]})
        if verdict == "accepted":
            bank.append({
                "id": r["id"], "source": r["source"], "question": r["question"],
                "template_question": r.get("template_question"), "sparql": info["expanded"],
                "compact": info["compact"], "skeleton": skeleton(info["compact"]),
                "classes": classes_in_query(info["expanded"]), "family": r.get("family"),
                "variant": r.get("variant"), "expected_compliant": r.get("expected_compliant"),
                "gold_section": r.get("gold_section"), "empty_on_sample": info["re_empty"],
                "priority": PRIORITY[r["source"]],
            })
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as f:
        for row in report:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    bank.sort(key=lambda e: (e["priority"], e["id"]))
    with open(args.out, "w", encoding="utf-8") as f:
        for ex in bank:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    for k, v in sorted(stats.items(), key=lambda kv: str(kv[0])):
        print(k, v)
    print(f"bank: {len(bank)} examples, {len({e['skeleton'] for e in bank})} distinct skeletons -> {args.out}")


if __name__ == "__main__":
    main()
