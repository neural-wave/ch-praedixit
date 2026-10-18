"""Layer 1: fill every recipe from the profile, run each query on S, keep the ones that pass -> data/bases.jsonl."""
import json
import random
import time

from common import CFG, DATA, GLOBAL, pmap, sparql, write_jsonl
from recipes import RECIPES, Profile
from rules import check

rng = random.Random(CFG["seed"])
pf = Profile(json.loads((DATA / "profile.json").read_text()), rng)

# anchor: the recipe's inference-based code match must count the same patients as an explicit hierarchy walk
for code in ["icd-10-gm:A00", "icd-10-gm:E11"]:
    mine = sparql(f"SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ ?r a sphn:BilledDiagnosis ; sphn:hasCode ?c . ?c isa {code} . }} }}")[1]
    ref = sparql(f"""SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ ?r a sphn:BilledDiagnosis ; sphn:hasCode ?c . ?c a ?x }}
                     GRAPH <{GLOBAL}> {{ ?x rdfs:subClassOf* {code} }} }}""", timeout=600)[1]
    print(f"anchor {code}: recipe {mine[0][0]} patients, hierarchy walk {ref[0][0]}", "OK" if mine == ref else "MISMATCH")

candidates = []
for name, fn in RECIPES.items():
    seen = set()
    for _ in range(CFG["fills_per_recipe"] * 5):
        if len(seen) >= CFG["fills_per_recipe"]:
            break
        try:
            c, query, meaning, shape = fn(pf)
        except (TypeError, IndexError, KeyError, ValueError):
            continue  # this recipe found no fitting slot this time
        if query not in seen:
            seen.add(query)
            candidates.append({"base_id": f"{name[:3]}-{len(seen):04d}", "recipe": name,
                               "slots": {"key": list(c["key"]), "codes": c["codes"]}, "zero_ok": c["zero_ok"],
                               "meaning": meaning, "shape": shape, "query": query})
    print(f"{name}: {len(seen)} fills")


def run(b):
    t = time.time()
    _, rows = sparql(b["query"])
    b["ms"] = round(1000 * (time.time() - t))
    b["result_on_S"] = rows[:20]
    return b


bases, dropped = [], {"error": 0, "zero": 0, "rules": 0}
for b in pmap(run, candidates):
    if b is None:
        dropped["error"] += 1
    elif not b["zero_ok"] and all(v in ("", "0") for r in b["result_on_S"] for v in r):
        dropped["zero"] += 1
    elif check(b["query"]):
        dropped["rules"] += 1
        print("  rules flag an allowed recipe (bug in recipe or rules):", b["recipe"], check(b["query"]))
    else:
        bases.append(b)
write_jsonl(DATA / "bases.jsonl", bases)
print(f"bases.jsonl: {len(bases)} allowed base queries; dropped {dropped}")
