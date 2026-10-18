import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)
CFG = yaml.safe_load((HERE / "config.yaml").read_text())

GLOBAL = "https://biomedit.ch/rdf/sphn-resource/global"
PREFIXES = {
    "sphn": "https://biomedit.ch/rdf/sphn-schema/sphn#",
    "sphn-individual": "https://biomedit.ch/rdf/sphn-schema/sphn/individual#",
    "resource": "https://biomedit.ch/rdf/sphn-resource/",
    "icd-10-gm": "https://biomedit.ch/rdf/sphn-resource/icd-10-gm/",
    "chop": "https://biomedit.ch/rdf/sphn-resource/chop/",
    "ucum": "https://biomedit.ch/rdf/sphn-resource/ucum/",
    "sphn-atc": "https://biomedit.ch/rdf/sphn-resource/atc/",
    "snomed": "http://snomed.info/id/",
    "loinc": "https://loinc.org/rdf/",
    "geno": "http://purl.obolibrary.org/obo/GENO_",
    "so": "http://purl.obolibrary.org/obo/SO_",
    "obi": "http://purl.obolibrary.org/obo/OBI_",
    "ordo": "http://www.orpha.net/ORDO/",
    "efo": "http://www.ebi.ac.uk/efo/EFO_",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "bif": "bif:",
}
NS_NAMES = {"icd-10-gm": "ICD-10-GM", "chop": "CHOP", "snomed": "SNOMED CT", "loinc": "LOINC",
            "sphn-atc": "ATC", "ucum": "UCUM", "ordo": "ORDO", "geno": "GENO", "so": "SO", "obi": "OBI", "efo": "EFO"}
LABELS = ["aggregate", "single_patient", "individual_rows", "identifiers", "whole_graph", "non_select"]


# ---------- shorthand <-> full SPARQL ----------

def short(iri):
    """Full IRI -> prefixed name when it is valid SPARQL, else <iri>."""
    for p, ns in PREFIXES.items():
        if iri.startswith(ns) and p != "resource":
            local = iri[len(ns):]
            if re.fullmatch(r"[\w\-]([\w.\-]*[\w\-])?", local):
                return f"{p}:{local}"
    return f"<{iri}>"


def expand(q):
    """Shorthand -> runnable Virtuoso query: `?c isa X` becomes an inferred rdf:type, PREFIX lines are added."""
    q = re.sub(r"\bisa\s+([\w\-]+:[\w.\-]*[\w\-]|<[^>]+>)",
               rf'rdf:type \1 OPTION (inference "{GLOBAL}")', q)
    used = [p for p in PREFIXES if re.search(rf"(?<![\w\-]){re.escape(p)}:", q)]
    return "".join(f"PREFIX {p}: <{PREFIXES[p]}>\n" for p in used) + q


def to_shorthand(q):
    q = re.sub(r"(?im)^\s*(PREFIX|DEFINE)\b.*\n", "", q)
    q = re.sub(r"\b(?:a|rdf:type)\s+(\S+)\s+OPTION\s*\(\s*inference\s+\"[^\"]+\"\s*\)", r"isa \1", q)
    return q.strip()


# ---------- Virtuoso ----------

def sparql(q, timeout=None):
    """Runs a shorthand or full SELECT; returns (columns, rows) with plain string values."""
    r = requests.post(CFG["endpoint"], data={"query": expand(q) if "PREFIX" not in q else q},
                      headers={"Accept": "application/sparql-results+json"},
                      timeout=timeout or CFG["query_timeout_s"])
    if not r.ok:
        raise RuntimeError(re.sub(r"\s+", " ", r.text)[:200])
    res = r.json()
    cols = res["head"]["vars"]
    return cols, [[b.get(c, {}).get("value", "") for c in cols] for b in res["results"]["bindings"]]


def sparql_all(q, page=10000):
    """Pages through a query that may exceed Virtuoso's row cap (needs an ORDER BY)."""
    rows, off = [], 0
    while True:
        cols, part = sparql(f"{q}\nLIMIT {page} OFFSET {off}", timeout=600)
        rows += part
        if len(part) < page:
            return [dict(zip(cols, r)) for r in rows]
        off += page


def same_result(a, b):
    """Order- and column-order-insensitive comparison, numbers rounded."""
    def norm(rows):
        def v(x):
            try:
                return f"{float(x):.2f}"
            except ValueError:
                return x
        return sorted(tuple(sorted(v(x) for x in r)) for r in rows)
    return norm(a) == norm(b)


# ---------- local LLMs (Ollama) ----------

def ask(role, prompt):
    """One chat call returning parsed JSON (the prompt must ask for a JSON object). No Ollama `format` option:
    some engines (e.g. MLX builds) answer 501 to it, and the JSON is cut out of the reply text anyway."""
    m = CFG[role]
    r = requests.post(f"{CFG['ollama']}/api/chat", timeout=900, json={
        "model": m["model"], "stream": False, "think": m["think"],
        "messages": [{"role": "user", "content": prompt}],
        "options": {"temperature": m["temperature"], "num_ctx": 8192}})
    if not r.ok:
        raise RuntimeError(f"Ollama {r.status_code}: {r.text[:200]}")
    text = r.json()["message"]["content"]
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


def embed(texts):
    out = []
    for i in range(0, len(texts), 256):
        r = requests.post(f"{CFG['ollama']}/api/embed", timeout=600,
                          json={"model": CFG["embedder"], "input": texts[i:i + 256]})
        out += r.json()["embeddings"]
    return out


def pmap(fn, items):
    """Runs fn over items in parallel; a failing item yields None. Prints progress."""
    def safe(x):
        try:
            return fn(x)
        except Exception as e:
            print("  skipped:", type(e).__name__, str(e)[:120])
    with ThreadPoolExecutor(CFG["workers"]) as ex:
        for i, out in enumerate(ex.map(safe, items), 1):
            if i % 25 == 0:
                print(f"  {i}/{len(items)}")
            yield out


# ---------- files and splits ----------

def read_jsonl(name):
    p = DATA / name
    return [json.loads(l) for l in p.open()] if p.exists() else []


def append_jsonl(name, rows):
    with (DATA / name).open("a") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_jsonl(path, rows):
    with Path(path).open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def split_of(base_id, query):
    """Fixed per base, so a base's wordings and twins always land in the same split."""
    if any(c in query for c in CFG["held_out_classes"]):
        return "test_t2"
    h = int(hashlib.md5(base_id.encode()).hexdigest(), 16) % 10000 / 10000
    if h < CFG["t3_share"]:
        return "test_t3"
    return "dev" if h < CFG["t3_share"] + CFG["dev_share"] else "train"


def words(name):
    """sphn:BilledDiagnosis -> 'billed diagnosis', sphn:hasAdministrationRouteCode -> 'administration route code'."""
    local = re.sub(r"^.*[#:/]", "", name)
    local = re.sub(r"^has", "", local)
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", local).lower()


OFFICIAL_LABELS = {11: "single_patient", 12: "individual_rows", 13: "whole_graph", 14: "non_select", 15: "identifiers"}


def official_examples():
    """The 15 examples of queries-example.md as {n, request, query (shorthand), original, label}."""
    text = (HERE / CFG["examples_md"]).read_text()
    parts = re.split(r"(?m)^(\d+)\.\s+.*$", text)[1:]
    out = []
    for n, body in zip(parts[::2], parts[1::2]):
        request = re.search(r'"(.+?)"', body).group(1)
        query = body[body.index(request) + len(request) + 1:]
        out.append({"n": int(n), "request": request, "query": to_shorthand(query), "original": query.strip(),
                     "label": OFFICIAL_LABELS.get(int(n), "aggregate")})
    return out
