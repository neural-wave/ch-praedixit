"""Layer 3: forbidden twins. Each allowed base gets one twin: a program edits its query to break exactly one rule
(so the label is known for certain), the teacher rewrites the question to ask for the edited thing, the judge checks
it, and the rules must flag the twin on the expected label. Plus the small whole-graph family (E7).
Twin queries are never run. -> data/rows_forbidden.jsonl (resumable)."""
import json
import random
import re

from common import CFG, DATA, append_jsonl, ask, pmap, read_jsonl, split_of, words
from recipes import Profile
from rules import _block, label_of, tainted_vars
from s4_wording import judge_prompt, style, style_line

rng = random.Random(CFG["seed"] + 1)
pf = Profile(json.loads((DATA / "profile.json").read_text()), rng)


def core_of(q):
    """The first patient-graph block of a query, its main record variable and that record's class."""
    m = re.search(r"GRAPH\s+\?\w+\s*\{", q)
    core = _block(q, m.end() - 1).strip()
    r = re.search(r"\?(\w+)\s+(?:a|isa|[\w\-]+:)", core).group(1)
    cls = re.search(rf"\?{r}\s+a\s+(sphn:\w+)", core)
    return core, r, cls.group(1) if cls else None


def patient():
    n = rng.randint(1000001, 1100000)
    return n, rng.choice([f"gen_mock_data-1_{n}", f"mock_data-1_{n % 10000:04d}", f"patient-{n % 100000}"])


def e1(core, r, cls):
    n, name = patient()
    return ("single_patient", f"SELECT (COUNT(DISTINCT ?{r}) AS ?records) WHERE {{ GRAPH <http://example.org/graph/{name}> {{ {core} }} }}",
            f"Counts the matching records of one single patient, patient {name.split('_')[-1].split('-')[-1]} "
            f"(graph {name}), instead of a result across all patients.")


def e2(core, r, cls):
    n, _ = patient()
    pid = f"ID-{n}{rng.randint(1, 99):07d}"
    return ("single_patient", f'SELECT (COUNT(DISTINCT ?{r}) AS ?records) WHERE {{ GRAPH ?g {{ {core} '
            f'?{r} sphn:hasSubjectPseudoIdentifier ?p . ?p sphn:hasIdentifier "{pid}" . }} }}',
            f"Checks one single patient, identified by the ID {pid}, and counts their matching records.")


def e3(core, r, cls):
    dates = [p for p in pf.props(cls) if p.endswith("DateTime")] if cls else []
    if not dates:
        return None
    d = rng.choice(dates)
    values = sorted(set(re.findall(r"\?(\w+)", core)) - tainted_vars(f"GRAPH ?g {{ {core} }}"))
    cols = " ".join(f"?{v}" for v in ["when"] + values[:3])
    return ("individual_rows", f"SELECT {cols} WHERE {{ GRAPH ?g {{ {core} ?{r} {d} ?when . }} }} ORDER BY ?when",
            f"Lists every matching record one by one, with its {words(d)}"
            + (f" and {', '.join(values[:3])}" if values else "") + ", instead of aggregates across patients.")


def e4(core, r, cls):
    if rng.random() < 0.5:
        return ("identifiers", f"SELECT DISTINCT ?g WHERE {{ GRAPH ?g {{ {core} }} }}",
                "Lists every patient (their patient graph) that matches, instead of counting them.")
    return ("identifiers", f"SELECT DISTINCT ?id WHERE {{ GRAPH ?g {{ {core} ?{r} sphn:hasSubjectPseudoIdentifier ?p . "
            f"?p sphn:hasIdentifier ?id . }} }}", "Lists the identifier of every matching patient, instead of counting them.")


def e5(core, r, cls):
    agg = rng.choice(['GROUP_CONCAT(DISTINCT STR(?g); separator=", ") AS ?which_patients', "SAMPLE(?g) AS ?example_patient"])
    return ("identifiers", f"SELECT (COUNT(DISTINCT ?g) AS ?patients) ({agg}) WHERE {{ GRAPH ?g {{ {core} }} }}",
            "Counts the matching patients and also says which patients they are"
            + (" (all of them)." if "GROUP" in agg else " (one example patient)."))


def e6(core, r, cls):
    form = rng.choice(["construct", "describe", "ask", "delete"])
    return ("non_select", {
        "construct": f"CONSTRUCT {{ ?{r} ?p ?o }} WHERE {{ GRAPH ?g {{ {core} ?{r} ?p ?o . }} }}",
        "describe": f"DESCRIBE ?{r} WHERE {{ GRAPH ?g {{ {core} }} }}",
        "ask": f"ASK {{ GRAPH ?g {{ {core} }} }}",
        "delete": f"DELETE {{ GRAPH ?g {{ ?{r} ?p ?o }} }} WHERE {{ GRAPH ?g {{ {core} ?{r} ?p ?o . }} }}"}[form], {
        "construct": "Exports all the matching records with all their data as an RDF graph to take away.",
        "describe": "Returns the full description (all data) of every matching record.",
        "ask": "Answers only yes or no: is there at least one matching patient?",
        "delete": "Deletes all the matching records from the database."}[form])


EDITS = {"E1": e1, "E2": e2, "E3": e3, "E4": e4, "E5": e5, "E6": e6}
WHOLE_GRAPH = [
    ("SELECT (COUNT(*) AS ?triples) (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?s ?p ?o } }",
     "Counts all triples in all patient graphs and the number of patient graphs: how much data there is in total."),
    ("SELECT ?p (COUNT(*) AS ?uses) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?p ORDER BY DESC(?uses)",
     "Counts how often every property is used across all data, without restricting to any kind of record."),
    ("SELECT (COUNT(DISTINCT ?s) AS ?nodes) WHERE { GRAPH ?g { ?s ?p ?o } }",
     "Counts every distinct node in all patient data, of any kind."),
    ("SELECT (AVG(?n) AS ?avg_triples) WHERE { { SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g } }",
     "Gives the average amount of data (triples) stored per patient, over everything."),
    ("SELECT ?type (COUNT(DISTINCT ?s) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o . OPTIONAL { ?s a ?type } } } GROUP BY ?type",
     "Counts every node in the database grouped by its type, scanning all data without restriction."),
]


def twin_prompt(original, meaning, styles):
    qs = "\n".join(f"Q{i + 1}: {style_line(s)}" for i, s in enumerate(styles))
    keys = ", ".join(f'"q{i + 1}": "..."' for i in range(len(styles)))
    return f"""You write requests that users type into a hospital data tool. Some users ask for things they are not
allowed to get; we need realistic examples of those to train a privacy filter.

An allowed request on the same topic: "{original}"
Write {len(styles)} requests where the user asks for THIS instead: {meaning}
Keep the medical topic of the allowed request. The request must clearly ask for exactly that, as a real person would.
{qs}
Answer as JSON: {{{keys}}}"""


def word_twin(original, meaning, n):
    styles = [style() for _ in range(n)]
    out = ask("teacher", twin_prompt(original, meaning, styles))
    qs = [out.get(f"q{i + 1}", "") for i in range(n)]
    verdicts = ask("judge", judge_prompt(meaning, qs))["verdicts"]
    return [(q, s, v) for q, s, v in zip(qs, styles, verdicts)]


def run(item):
    base, original = item
    core, r, cls = core_of(base["query"])
    names, weights = zip(*CFG["twin_edits"].items())
    for name in rng.choices(names, weights, k=3):  # retry when an edit does not fit this base
        if out := EDITS[name](core, r, cls):
            break
    else:
        return [], []
    label, query, meaning = out
    common = {"base_id": base["base_id"], "recipe": base["recipe"], "edit": name, "meaning": meaning, "query": query,
              "label": label, "compliant": False, "split": split_of(base["base_id"], base["query"])}
    if label_of(query) != label:
        return [], [{**common, "stage": "twin rules disagree", "rules_label": label_of(query)}]
    rows, rejected = [], []
    for i, (q, s, v) in enumerate(word_twin(original, meaning, rng.choice([1, 2]))):
        if v.get("match") and q:
            rows.append({"id": f"{base['base_id']}-{name}-w{i}", **common, "request": q, "style": s,
                         "source": f"{name} edit + {CFG['teacher']['model']} wording"})
        else:
            rejected.append({**common, "request": q, "problem": v.get("problem", ""), "stage": "twin wording"})
    return rows, rejected


def run_whole_graph(i):
    query, meaning = rng.choice(WHOLE_GRAPH)
    styles = [style()]
    out = ask("teacher", twin_prompt("How many patients have type 2 diabetes?", meaning, styles))
    q = out.get("q1", "")
    v = ask("judge", judge_prompt(meaning, [q]))["verdicts"][0]
    row = {"id": f"E7-{i:04d}", "base_id": f"E7-{i:04d}", "recipe": "whole_graph", "edit": "E7", "meaning": meaning,
           "query": query, "label": "whole_graph", "compliant": False, "request": q, "style": styles[0],
           "source": f"E7 template + {CFG['teacher']['model']} wording", "split": split_of(f"E7-{i:04d}", query)}
    return ([row], []) if v.get("match") and q else ([], [{**row, "stage": "twin wording", "problem": v.get("problem", "")}])


if __name__ == "__main__":
    done = {r["base_id"] for r in read_jsonl("rows_forbidden.jsonl") + read_jsonl("rejected.jsonl") if "edit" in r}
    first = {}
    for row in read_jsonl("rows_allowed.jsonl"):
        first.setdefault(row["base_id"], row["request"])
    bases = [(b, first[b["base_id"]]) for b in read_jsonl("bases.jsonl") + read_jsonl("bases_freeform.jsonl")
             if b["base_id"] in first and b["base_id"] not in done]
    todo_e7 = [i for i in range(CFG["whole_graph_wordings"]) if f"E7-{i:04d}" not in done]
    print(f"twins for {len(bases)} bases, {len(todo_e7)} whole-graph rows")
    for fn, items in [(run, bases), (run_whole_graph, todo_e7)]:
        for out in pmap(fn, items):
            if out:
                append_jsonl("rows_forbidden.jsonl", out[0])
                append_jsonl("rejected.jsonl", out[1])
    rows = read_jsonl("rows_forbidden.jsonl")
    print(f"rows_forbidden.jsonl: {len(rows)} rows;",
          sum(r.get("stage") == "twin rules disagree" for r in read_jsonl("rejected.jsonl")), "edits the rules disagreed with")
