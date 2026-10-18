"""Layer 4: the teacher writes new analytic questions with queries beyond the recipes. An item is kept only if it
runs on S with a non-trivial result, passes the rules, and the judge model, writing its own query from the question
alone, gets the same result. Appends to data/bases_freeform.jsonl (running again adds more)."""
import json
import random

from common import CFG, DATA, append_jsonl, ask, pmap, read_jsonl, same_result, sparql
from recipes import RULES_TEXT, Profile, cards
from rules import check

pf = Profile(json.loads((DATA / "profile.json").read_text()), random.Random(CFG["seed"] + len(read_jsonl("bases_freeform.jsonl"))))
examples = [e for e in read_jsonl("seeds.jsonl") if e["label"] == "aggregate"]


def prompt():
    ex = pf.rng.sample(examples, 2)
    c = cards(pf, pf.rng.randint(4, 6))
    return c, f"""You write analytic questions and SPARQL queries for an SPHN hospital database (Virtuoso).
{RULES_TEXT}

Concepts that exist in this database:
{c}

Two worked examples:
Q: {ex[0]['request']}
{ex[0]['query']}

Q: {ex[1]['request']}
{ex[1]['query']}

Write 5 NEW questions a medical researcher would ask that need the concepts above, each with its query and a
one-sentence MEANING that states exactly what the query computes. Results must be aggregated across patients.
Answer as JSON: {{"items": [{{"question": "...", "query": "...", "meaning": "..."}}, ...]}}"""


def nontrivial(rows):
    return rows and not all(v in ("", "0") for r in rows for v in r)


def verify(item, c):
    if check(item["query"]):
        return None
    cols, rows = sparql(item["query"])
    if not nontrivial(rows):
        return None
    other = ask("judge", f"""Write one SPARQL query that answers the question below.
{RULES_TEXT}

Concepts that exist in this database:
{c}

QUESTION: {item['question']}
Answer as JSON: {{"query": "..."}}""")["query"]
    if not same_result(rows, sparql(other)[1]):
        return None
    return {"recipe": "free_form", "slots": {}, "zero_ok": False, "meaning": item["meaning"],
            "shape": f"{len(rows)} row(s) with columns {', '.join(cols)}", "query": item["query"],
            "question": item["question"], "judge_query": other, "result_on_S": rows[:20]}


def one(_):
    c, p = prompt()
    kept, items = [], ask("teacher", p)["items"]
    for item in items:
        try:
            if v := verify(item, c):
                kept.append(v)
        except Exception:
            pass  # does not parse or run: dropped
    return kept, len(items)


n0 = len(read_jsonl("bases_freeform.jsonl"))
written = kept = 0
for out in pmap(one, range(CFG["freeform_calls"])):
    if out:
        written += out[1]
        for b in out[0]:
            n0 += 1
            b["base_id"] = f"FF-{n0:04d}"
            append_jsonl("bases_freeform.jsonl", [b])
            kept += 1
print(f"free-form: {kept} of {written} written items survived all four checks")
