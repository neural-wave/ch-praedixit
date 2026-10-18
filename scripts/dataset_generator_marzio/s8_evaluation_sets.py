"""Exports data/dataset_v1 into ../evaluation_marzio, one folder per component:
generator/ (notes = the shorthand query it is trained to write) and compliance_checker/ (notes = the expanded query
it receives). Each holds train.jsonl (train + dev rows), test.jsonl (T1-T4) and rq_files/ (one .rq per row of both;
each jsonl row names its file in `rq_file`)."""
import enum
import json
import re

from common import DATA, HERE, expand, words


class Rule(str, enum.Enum):
    """A compliance rule; the values match `expected_rules` in the compliance test set."""

    NONE = "none"
    NOT_READ_ONLY = "not_read_only"
    NOT_SELECT = "not_select"
    NOT_AGGREGATED = "not_aggregated"
    EXPOSES_IDENTIFIER = "exposes_identifier"
    SINGLE_PATIENT = "single_patient"
    UNRESTRICTED_MATCH = "unrestricted_match"
    INVALID_SPARQL = "invalid_sparql"


RULE_OF = {"aggregate": Rule.NONE, "single_patient": Rule.SINGLE_PATIENT, "identifiers": Rule.EXPOSES_IDENTIFIER,
           "individual_rows": Rule.NOT_AGGREGATED, "whole_graph": Rule.UNRESTRICTED_MATCH}
ORIGIN = {"train": "train", "dev": "dev", "test_t1": "test set T1 (official example)",
          "test_t2": "test set T2 (held-out concept)", "test_t3": "test set T3 (worded by the other model)",
          "test_t4": "test set T4 (hand-written)"}
ASK_NOTE = "ASK: Query_Compliance.md allows only SELECT; API_specification.md lists ASK as read-only and valid."
OUT = HERE / "evaluation_marzio"


def verdict(row):
    """(expected_compliant, Rule) from the factory label; non_select is split by query form."""
    if row["label"] != "non_select":
        return row["label"] == "aggregate", RULE_OF[row["label"]]
    m = re.search(r"\b(ASK|CONSTRUCT|DESCRIBE|INSERT|DELETE|LOAD|CLEAR|DROP|CREATE)\b", row.get("query") or "")
    if m and m.group(1) not in ("ASK", "CONSTRUCT", "DESCRIBE"):
        return False, Rule.NOT_READ_ONLY
    # official example 14 marks CONSTRUCT non-compliant; ASK is an open question to the organisers
    return ("ambiguous" if m and m.group(1) == "ASK" else False), Rule.NOT_SELECT


def request_source(row):
    if row["source"] == "official example":
        return "official_example"
    if row.get("recipe") == "hard_case":
        return "hard_case"
    if (row.get("style") or {}).get("source") == "free-form question":
        return "free_form_question"
    return "paraphrase_other_model" if row["split"] == "test_t3" else "paraphrase"


def variant(row):
    if row["source"] == "official example":
        return "official"
    if row.get("edit"):
        return f"twin_{row['edit']}"
    return f"hard_{row['category']}" if row.get("category") else "base"


def concept(row):
    key = (row.get("slots") or {}).get("key")
    cls = key[0] if key else (re.findall(r"\b(?:a|isa)\s+(sphn:\w+)", row["query"]) or [""])[0]
    return words(cls) if cls else ""


def fills(row):
    out = []
    for v in (row.get("slots") or {}).values():
        out += v if isinstance(v, list) else [v]
    return [str(x) for x in out]


def one_line(q):
    return re.sub(r"\s+", " ", q).strip()


def record(row, i, kind, notes):
    ok, rule = verdict(row)
    return {"id": f"{kind}-{i:04d}", "split": kind if kind == "test" else row["split"], "request": row["request"],
            "sparql": expand(row.get("query") or ""), "expected_compliant": ok, "expected_rule": rule.value,
            "request_kind": "request", "request_source": request_source(row),
            "request_template_version": row.get("meaning", row["request"]), "family": row.get("recipe", "official"),
            "concept": concept(row), "variant": variant(row), "template": None, "fills": fills(row),
            "origin": ORIGIN[row["split"]], "notes": notes(row), "source_id": row["id"]}


def rq(rec, row):
    notes = rec["notes"] + (f" | {ASK_NOTE}" if rec["expected_compliant"] == "ambiguous" else "")
    head = [f"question: {one_line(rec['request'])}", f"expected_compliant: {str(rec['expected_compliant']).lower()}",
            f"expected_rules: {rec['expected_rule']}", f"origin: {rec['origin']}", f"notes: {notes}"]
    return "".join(f"# {h}\n" for h in head) + expand(row["query"]).strip() + "\n"


def read(name):
    p = DATA / "dataset_v1" / f"{name}.jsonl"
    return [json.loads(l) for l in p.open()] if p.exists() else []


train = read("train") + read("dev")
test = read("test_t1") + read("test_t2") + read("test_t3") + read("test_t4")
NOTES = {"generator": lambda r: one_line(r.get("query") or ""),
         "compliance_checker": lambda r: one_line(expand(r.get("query") or ""))}
for folder, notes in NOTES.items():
    d = OUT / folder
    (d / "rq_files").mkdir(parents=True, exist_ok=True)
    n = 0  # .rq files are numbered across train then test: single_example_t1 ... t<all rows>
    for kind, rows in (("train", train), ("test", test)):
        recs = []
        for i, r in enumerate(rows, 1):
            rec = record(r, i, kind, notes)
            n += 1
            if r.get("query"):  # hand-written T4 rows may come without a query
                rec["rq_file"] = f"single_example_t{n}.rq"
                (d / "rq_files" / rec["rq_file"]).write_text(rq(rec, r))
            recs.append(rec)
        (d / f"{kind}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))
    print(f"{d}: {len(train)} train rows, {len(test)} test rows, {n} .rq files")

counts = {}
for r in train + test:
    counts[verdict(r)] = counts.get(verdict(r), 0) + 1
for (ok, rule), n in sorted(counts.items(), key=lambda x: -x[1]):
    print(f"  {rule.value:<20} expected_compliant={ok}: {n}")
