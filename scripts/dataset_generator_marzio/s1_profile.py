"""Layer 0: profile subset S -> data/profile.json (what really occurs, with English code labels), and the
seed set -> data/seeds.jsonl (official examples in shorthand, checked to return the official result on S)."""
import json
import re
from collections import defaultdict

from common import DATA, GLOBAL, PREFIXES, ask, official_examples, pmap, same_result, short, sparql, sparql_all, write_jsonl
from rules import label_of

NOT_GLOBAL = f"FILTER(?g != <{GLOBAL}>)"
VERSIONED = re.compile(r"/\d{4}/")
P = {"patients": int(sparql(f"SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ ?s a sphn:SubjectPseudoIdentifier }} {NOT_GLOBAL} }}")[1][0][0])}
print("patients in S:", P["patients"])

P["classes"] = {short(r["class"]): {"patients": int(r["patients"]), "records": int(r["records"])} for r in sparql_all(f"""
SELECT ?class (COUNT(DISTINCT ?g) AS ?patients) (COUNT(*) AS ?records)
WHERE {{ GRAPH ?g {{ ?r a ?class }} {NOT_GLOBAL} FILTER(STRSTARTS(STR(?class), STR(sphn:))) }}
GROUP BY ?class ORDER BY ?class""")}

P["props"] = defaultdict(dict)
for r in sparql_all(f"""
SELECT ?class ?prop (COUNT(DISTINCT ?g) AS ?patients)
WHERE {{ GRAPH ?g {{ ?r a ?class ; ?prop ?o }} {NOT_GLOBAL}
         FILTER(STRSTARTS(STR(?class), STR(sphn:)) && STRSTARTS(STR(?prop), STR(sphn:))) }}
GROUP BY ?class ?prop ORDER BY ?class ?prop"""):
    P["props"][short(r["class"])][short(r["prop"])] = int(r["patients"])

# coded attributes, with yearly versions folded into the current code (…/icd-10-gm/2018/C91.0 -> icd-10-gm:C91.0).
# Folding happens here, not in the query: grouping on a computed string fails on larger data (Virtuoso SR319).
# Summing over versions overcounts a patient who has several versions of one code, hence the cap.
codes = defaultdict(int)
for r in sparql_all(f"""
SELECT ?class ?prop ?concept (COUNT(DISTINCT ?g) AS ?patients)
WHERE {{ GRAPH ?g {{ ?r a ?class ; ?prop ?c . ?c a ?concept }} {NOT_GLOBAL}
         FILTER(STRSTARTS(STR(?class), STR(sphn:)) && STRENDS(STR(?prop), "Code"))
         FILTER(!STRSTARTS(STR(?concept), STR(sphn:))) }}
GROUP BY ?class ?prop ?concept ORDER BY ?class ?prop ?concept"""):
    codes[short(r["class"]), short(r["prop"]), short(VERSIONED.sub("/", r["concept"]))] += int(r["patients"])
P["codes"] = [{"class": cls, "prop": prop, "code": code, "patients": min(n, P["patients"])}
              for (cls, prop, code), n in codes.items() if not code.startswith("<")]

P["individuals"] = [{"class": short(r["class"]), "prop": short(r["prop"]), "value": short(r["val"]), "patients": int(r["patients"])}
                    for r in sparql_all(f"""
SELECT ?class ?prop ?val (COUNT(DISTINCT ?g) AS ?patients)
WHERE {{ GRAPH ?g {{ ?r a ?class ; ?prop ?val }} {NOT_GLOBAL}
         FILTER(STRSTARTS(STR(?class), STR(sphn:)) && STRSTARTS(STR(?val), STR(sphn-individual:))) }}
GROUP BY ?class ?prop ?val ORDER BY ?class ?prop ?val""")]

P["measures"] = [{"class": short(r["class"]), "unit": short(r["unit"]), "patients": int(r["patients"]), "n": int(r["n"]),
                  "min": float(r["min"]), "avg": float(r["avg"]), "max": float(r["max"])}
                 for r in sparql_all(f"""
SELECT ?class ?unit (COUNT(DISTINCT ?g) AS ?patients) (COUNT(*) AS ?n) (MIN(?v) AS ?min) (AVG(?v) AS ?avg) (MAX(?v) AS ?max)
WHERE {{ GRAPH ?g {{ ?r a ?class ; sphn:hasQuantity ?q . ?q sphn:hasValue ?v ; sphn:hasUnit/sphn:hasCode ?u . ?u a ?unit }}
         {NOT_GLOBAL} FILTER(STRSTARTS(STR(?class), STR(sphn:)) && ISNUMERIC(?v)) }}
GROUP BY ?class ?unit ORDER BY ?class ?unit""")]
P["measures"] = [m for m in P["measures"] if not m["unit"].startswith("<")]


def full(code):
    p, local = code.split(":", 1)
    return PREFIXES[p] + local


def edges(values_var, codes):
    """subClassOf edges in the ontology graph between current (unversioned) codes, for a VALUES list."""
    out = []
    codes = list(codes)
    for i in range(0, len(codes), 200):
        vals = " ".join(f"<{full(c)}>" for c in codes[i:i + 200])
        out += sparql(f"""SELECT ?c ?p WHERE {{ GRAPH <{GLOBAL}> {{ VALUES ?{values_var} {{ {vals} }}
                          ?c rdfs:subClassOf ?p }} }}""", timeout=300)[1]
    return [(short(c), short(p)) for c, p in out if not VERSIONED.search(c + p) and "<" not in short(c) + short(p)]


# hierarchy for ICD-10-GM and CHOP: parents up to 3 levels, and sibling codes absent from S
present = {c["code"] for c in P["codes"] if c["code"].split(":")[0] in ("icd-10-gm", "chop")}
P["parents"], level = defaultdict(list), present
for _ in range(3):
    for c, p in set(edges("c", [c for c in level if c not in P["parents"]])):
        if not re.search(r":(ICD-10-GM|CHOP|\d\d|C\d+)$", p):  # no roots or whole chapters (too broad and slow)
            P["parents"][c].append(p)
    level = {p for c in level for p in P["parents"].get(c, [])}
all_parents = {p for ps in P["parents"].values() for p in ps}
P["absent"] = defaultdict(list)
for c, p in edges("p", all_parents):
    if c not in present and c not in all_parents and len(P["absent"][c.split(":")[0]]) < 300:
        P["absent"][c.split(":")[0]].append(c)

# parent codes get an estimated patient count (sum over children) so recipes can sample them too
est = defaultdict(int)
for c in P["codes"]:
    todo = list(P["parents"].get(c["code"], []))
    while todo:
        p = todo.pop()
        est[(c["class"], c["prop"], p)] += c["patients"]
        todo += P["parents"].get(p, [])
seen = {(c["class"], c["prop"], c["code"]) for c in P["codes"]}
P["codes"] += [{"class": k[0], "prop": k[1], "code": k[2], "patients": min(n, P["patients"]), "parent": True}
               for k, n in est.items() if k not in seen]

# labels: English where the ontology has it, else the teacher translates the German/French one
needed = list({c["code"] for c in P["codes"]} | {c for v in P["absent"].values() for c in v} | {m["unit"] for m in P["measures"]})
raw = defaultdict(dict)
for i in range(0, len(needed), 200):
    vals = " ".join(f"<{full(c)}>" for c in needed[i:i + 200])
    for c, l, lang in sparql(f"""SELECT ?c ?l (LANG(?l) AS ?lang)
                                 WHERE {{ GRAPH <{GLOBAL}> {{ VALUES ?c {{ {vals} }} ?c rdfs:label ?l }} }}""", timeout=300)[1]:
        raw[short(c)][lang] = l
# a code retired before 2026 has no current class: `isa` would never match it, so it is dropped
P["codes"] = [c for c in P["codes"] if c["code"].split(":")[0] not in ("icd-10-gm", "chop") or c["code"] in raw]
labels = {c: {"orig": ls.get("en") or ls.get("") or ls.get("de") or ls.get("fr")} for c, ls in raw.items()}
to_translate = [c for c, ls in raw.items() if not ("en" in ls or "" in ls)]
batches = [to_translate[i:i + 50] for i in range(0, len(to_translate), 50)]
print(f"translating {len(to_translate)} German/French labels in {len(batches)} calls")
for batch, out in zip(batches, pmap(lambda b: ask("teacher", (
        "Translate these German or French medical code labels into short, standard English clinical terms "
        "(as an English-speaking clinician would say them). Keep abbreviations in brackets.\n"
        + "\n".join(f"{c}: {labels[c]['orig']}" for c in b)
        + '\nAnswer as JSON: {"<code>": "<English label>", ...}')), batches)):
    for c in batch:
        labels[c]["en"] = (out or {}).get(c, labels[c]["orig"])
for c in labels:
    labels[c].setdefault("en", labels[c]["orig"])
P["labels"] = labels

(DATA / "profile.json").write_text(json.dumps(P, indent=1, ensure_ascii=False))
print(f"profile.json: {len(P['classes'])} classes, {len(P['codes'])} class/property/code entries, "
      f"{len(P['measures'])} measures, {len(labels)} labels")

# seed set: official examples in shorthand; compliant ones must give the official result on S
seeds = []
for e in official_examples():
    e["rules_label"] = label_of(e["query"])
    if e["label"] == "aggregate":
        try:  # a sanity check of the shorthand only: a slow example is reported, not fatal
            e["result_on_S"] = sparql(e["original"], timeout=600)[1]
            e["shorthand_ok"] = same_result(e["result_on_S"], sparql(e["query"], timeout=600)[1])
        except Exception as x:
            e["shorthand_ok"] = f"not checked ({type(x).__name__})"
    e.pop("original")
    seeds.append(e)
    print(f"  example {e['n']:>2}: label {e['label']:<15} rules {e['rules_label']:<15}"
          + (f" shorthand result matches: {e['shorthand_ok']}" if "shorthand_ok" in e else ""))
write_jsonl(DATA / "seeds.jsonl", seeds)
missing = {"sphn:" + c for e in seeds for c in re.findall(r"\ba sphn:(\w+)", e["query"])} - set(P["classes"])
print("classes used by the examples but absent from S:", sorted(missing) or "none")
