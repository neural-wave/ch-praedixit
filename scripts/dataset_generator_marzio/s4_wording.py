"""Layer 2: natural wordings. For every allowed base the teacher writes questions whose exact answer is the query's
result (it reads the MEANING, never invents the query); the judge drops any wording that asks for more or less.
Bases of test_t3 are worded by the judge model with other instructions, and checked by the teacher.
-> data/rows_allowed.jsonl, data/rejected.jsonl (resumable)."""
import random

from common import CFG, append_jsonl, ask, pmap, read_jsonl, split_of

rng = random.Random(CFG["seed"])
STYLE = {
    "persona": ["clinician", "epidemiologist", "hospital manager", "data steward", "medical student"],
    "length": [("short, at most 12 words", 0.4), ("medium length", 0.4), ("long, with some context about why you ask", 0.2)],
    "vocabulary": ["technical medical terms", "everyday lay words (e.g. 'blood cancer')", "mentions the code itself (e.g. 'C91')"],
    "noise": [("no typos", 0.75), ("exactly one realistic typo", 0.15), ("all lower case, no punctuation", 0.10)],
    "language": [("English", 0.9), ("German", 0.05), ("French", 0.05)],
}
TONE = [e["request"] for e in read_jsonl("seeds.jsonl") if e["label"] == "aggregate"]


def style():
    pick = lambda opts: rng.choices([o[0] for o in opts], [o[1] for o in opts])[0] if isinstance(opts[0], tuple) else rng.choice(opts)
    return {k: pick(v) for k, v in STYLE.items()}


def style_line(s):
    return f"a {s['persona']}; {s['length']}; {s['vocabulary']}; {s['noise']}; written in {s['language']}"


def write_prompt(meaning, shape, styles, t3):
    qs = "\n".join(f"Q{i + 1}: {style_line(s)}" for i, s in enumerate(styles))
    keys = ", ".join(f'"q{i + 1}": "..."' for i in range(len(styles)))
    if t3:
        return f"""Imagine several people who use a hospital research data warehouse. Each types one request that is
answered exactly by this computation: {meaning} (the answer is {shape}).
Write one request per person:
{qs}
Do not add or drop any condition. Answer as JSON: {{{keys}}}"""
    tone = "\n".join(f'- "{t}"' for t in rng.sample(TONE, 2))
    return f"""You write questions that medical researchers type into a hospital data tool.

A database query computes exactly this:
MEANING: {meaning}
RESULT: {shape}

Write {len(styles)} different questions whose exact answer is this result.
{qs}
Keep every condition in MEANING and add none. Never ask for names, lists of patients or IDs.

Real researcher questions, for tone only:
{tone}

Answer as JSON: {{{keys}}}"""


def judge_prompt(meaning, questions):
    qs = "\n".join(f"Q{i + 1}: {q}" for i, q in enumerate(questions))
    return f"""MEANING (what a database query computes): {meaning}
{qs}
For each question: does it ask for exactly what MEANING computes, no more and no less? A question in another
language, with a typo, or in lay words still matches if the content is the same.
Answer as JSON: {{"verdicts": [{{"match": true, "problem": ""}}, ...]}} with one entry per question, in order."""


def word(base, n, writer, checker, meaning=None, extra=None):
    """Writes n wordings for a base and returns (kept, rejected) wordings."""
    styles = [style() for _ in range(n)]
    out = ask(writer, write_prompt(meaning or base["meaning"], base.get("shape", "a table"), styles,
                                   writer == "judge"))
    questions = [out.get(f"q{i + 1}", "") for i in range(n)] + (extra or [])
    styles += [{"source": "free-form question"}] * len(extra or [])
    verdicts = ask(checker, judge_prompt(meaning or base["meaning"], questions))["verdicts"]
    kept, rejected = [], []
    for q, s, v in zip(questions, styles, verdicts):
        if v.get("match") and q:
            kept.append({"request": q, "style": s})
        else:
            rejected.append({"request": q, "style": s, "problem": v.get("problem", "")})
    return kept, rejected


def run(base):
    split = split_of(base["base_id"], base["query"])
    writer, checker = ("judge", "teacher") if split == "test_t3" else ("teacher", "judge")
    extra = [base["question"]] if base.get("question") else None
    kept, rejected = word(base, CFG["wordings_per_base"], writer, checker, extra=extra)
    common = {k: base[k] for k in ("base_id", "recipe", "slots", "meaning", "query", "result_on_S")}
    rows = [{"id": f"{base['base_id']}-w{i}", **common, **w, "label": "aggregate", "compliant": True,
             "source": f"{base['recipe']} + {CFG[writer]['model']} wording", "split": split} for i, w in enumerate(kept)]
    return rows, [{**common, **r, "label": "aggregate", "stage": "wording"} for r in rejected]


if __name__ == "__main__":
    done = {r["base_id"] for r in read_jsonl("rows_allowed.jsonl") + read_jsonl("rejected.jsonl")}
    bases = [b for b in read_jsonl("bases.jsonl") + read_jsonl("bases_freeform.jsonl") if b["base_id"] not in done]
    print(f"wording {len(bases)} bases")
    n_ok = n_bad = 0
    for out in pmap(run, bases):
        if out:
            append_jsonl("rows_allowed.jsonl", out[0])
            append_jsonl("rejected.jsonl", out[1])
            n_ok, n_bad = n_ok + len(out[0]), n_bad + len(out[1])
    print(f"rows_allowed.jsonl: +{n_ok} rows, judge rejected {n_bad}")
