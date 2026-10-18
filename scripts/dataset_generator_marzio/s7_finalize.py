"""Layer 6: rules on every row, dedup (exact and by embedding against the test sets), the human review sheet,
and the final splits -> data/dataset_v1/*.jsonl.
First run writes data/review.csv. Fill its `verdict` column (OK / wrong label / does not match) and run again:
rows marked wrong are dropped and the error rate per recipe is printed."""
import csv
import random
import re
from collections import Counter, defaultdict

import numpy as np

from common import CFG, DATA, embed, read_jsonl, write_jsonl
from rules import label_of

rng = random.Random(CFG["seed"])
rows = read_jsonl("rows_allowed.jsonl") + read_jsonl("rows_forbidden.jsonl") + read_jsonl("rows_hard.jsonl")
for r in rows:
    r["rules_label"] = label_of(r["query"])

tests = [{"id": f"T1-{e['n']:02d}", "request": e["request"], "query": e["query"], "label": e["label"],
          "compliant": e["label"] == "aggregate", "source": "official example", "split": "test_t1"}
         for e in read_jsonl("seeds.jsonl")]
tests += [{**r, "id": f"T4-{i:03d}", "compliant": r["label"] == "aggregate", "source": "hand-written", "split": "test_t4"}
          for i, r in enumerate(read_jsonl("t4_handwritten.jsonl"))]  # optional: your own {"request", "label"} lines

# exact duplicates
seen, unique = set(), []
for r in rows:
    key = re.sub(r"\W+", " ", r["request"].lower()).strip()
    if key and key not in seen:
        seen.add(key)
        unique.append(r)
print(f"{len(rows)} rows, {len(rows) - len(unique)} exact duplicates removed")
rows = unique

# near duplicates: training rows too close to any test request, then near-identical training rows
train = [r for r in rows if r["split"] in ("train", "dev")]
held = tests + [r for r in rows if r["split"] not in ("train", "dev")]
E = np.array(embed([r["request"] for r in train]), dtype=np.float32)
T = np.array(embed([r["request"] for r in held]), dtype=np.float32)
E /= np.linalg.norm(E, axis=1, keepdims=True)
T /= np.linalg.norm(T, axis=1, keepdims=True)
drop = (E @ T.T).max(axis=1) > CFG["dedup_cosine"]
print(f"{drop.sum()} training rows dropped as too close to a test request")
for a in range(0, len(E), 2000):
    S = E[a:a + 2000] @ E.T
    for i in range(a, min(a + 2000, len(E))):
        if not drop[i]:
            drop |= (S[i - a] > 0.97) & (np.arange(len(E)) > i)
print(f"{drop.sum()} training rows dropped in total after collapsing near-identical ones")
rows = [r for r, d in zip(train, drop) if not d] + [r for r in rows if r["split"] not in ("train", "dev")]

# human review
review = DATA / "review.csv"
if review.exists() and any(x["verdict"].strip() for x in csv.DictReader(review.open())):
    sheet = list(csv.DictReader(review.open()))
    wrong = {x["id"] for x in sheet if x["verdict"].strip() and x["verdict"].strip().lower() != "ok"}
    errors = defaultdict(lambda: [0, 0])
    for x in sheet:
        if x["kind"] == "random" and x["verdict"].strip():
            errors[x["recipe"]][0] += x["id"] in wrong
            errors[x["recipe"]][1] += 1
    for recipe, (bad, n) in sorted(errors.items()):
        print(f"  review {recipe}: {bad}/{n} wrong" + ("  <- above 5%, regenerate this group" if bad / n > 0.05 else ""))
    rows = [r for r in rows if r["id"] not in wrong]
    print(f"{len(wrong)} rows marked wrong in review.csv removed")
else:
    flagged = [r for r in rows if r["rules_label"] != r["label"] or r.get("needs_review")]
    rejected = [r for r in read_jsonl("rejected.jsonl") if r.get("request")]
    rejected = rng.sample(rejected, min(30, len(rejected)))
    pick = [("random", r) for r in rng.sample(rows, min(CFG["review_random"], len(rows)))]
    pick += [("flagged", r) for r in rng.sample(flagged, max(0, min(CFG["review_flagged"] - len(rejected), len(flagged))))]
    pick += [("judge-rejected", r) for r in rejected]
    with review.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "kind", "recipe", "request", "meaning", "label", "rules_label", "verdict"])
        for kind, r in pick:
            w.writerow([r.get("id", ""), kind, r["recipe"], r["request"], r.get("meaning", ""), r["label"],
                        label_of(r["query"]), ""])
    print(f"wrote {review} with {len(pick)} rows to review (fill `verdict`, then run this again)")

out = DATA / "dataset_v1"
out.mkdir(exist_ok=True)
by_split = defaultdict(list)
for r in rows + tests:
    by_split[r["split"]].append(r)
for split, part in sorted(by_split.items()):
    write_jsonl(out / f"{split}.jsonl", part)
    print(f"{split:>8}: {len(part):>6} rows  " + ", ".join(f"{k} {v}" for k, v in Counter(r["label"] for r in part).most_common()))
agree = sum(r["rules_label"] == r["label"] for r in rows) / max(1, len(rows))
print(f"rules agree with the label on {agree:.1%} of rows (misses are in review.csv as 'flagged')")
