"""Compliance rules (DESIGN.md C7) on query text. check(q) -> [] when compliant, else [(label, evidence)]."""
import re

from common import GLOBAL

AGG = r"\b(COUNT|SUM|AVG|MIN|MAX|SAMPLE|GROUP_CONCAT)\s*\("
LEAKY = r"\b(SAMPLE|GROUP_CONCAT|MIN|MAX)\s*\("


def _strip(q):
    q = re.sub(r"(?m)^\s*#.*$", "", q)
    return re.sub(r"(?im)^\s*(PREFIX|DEFINE)\b.*$", "", q)


def _block(text, start):
    """Text inside the balanced bracket opening at text[start]."""
    open_, close = text[start], {"{": "}", "(": ")"}[text[start]]
    depth = 0
    for i in range(start, len(text)):
        depth += (text[i] == open_) - (text[i] == close)
        if depth == 0:
            return text[start + 1:i]
    return text[start + 1:]


def _calls(expr, pattern):
    """Contents of every call matching pattern, e.g. the argument of GROUP_CONCAT(...)."""
    return [_block(expr, m.end() - 1) for m in re.finditer(pattern, expr, re.I)]


def _vars(text):
    return set(re.findall(r"\?(\w+)", text))


def _no_exists(text):
    out = text
    for m in reversed(list(re.finditer(r"\bEXISTS\s*\{", text, re.I))):
        out = out[:m.end() - 1] + "{}" + out[m.end() + len(_block(text, m.end() - 1)) + 1:]
    return out


def tainted_vars(q):
    """Variables that identify a patient or record: graph variables, record subjects, identifier values,
    and anything BIND copies them into."""
    t = set(re.findall(r"GRAPH\s+\?(\w+)", q, re.I))
    patient_part = q
    for m in reversed(list(re.finditer(r"GRAPH\s*<[^>]+>\s*\{", q, re.I))):  # ontology graph: concepts, not records
        patient_part = patient_part[:m.start()] + patient_part[m.end() + len(_block(q, m.end() - 1)) + 1:]
    t |= set(re.findall(r"(?:[{}]|\.\s)\s*\?(\w+)\s+(?:a\b|isa\b|[\w\-]+:|<|\?)", patient_part))
    t |= set(re.findall(r"sphn:has(?:Shared)?Identifier\s+\?(\w+)", q))
    for _ in range(3):
        for m in re.finditer(r"\bBIND\s*\(", q, re.I):
            body = _block(q, m.end() - 1)
            target = re.search(r"\bAS\s+\?(\w+)\s*$", body, re.I)
            if target and _vars(_no_exists(body[:target.start()])) & t:
                t.add(target.group(1))
    return t


def check(q):
    q = _strip(q)
    form = re.search(r"\b(SELECT|CONSTRUCT|DESCRIBE|ASK|INSERT|DELETE|LOAD|CLEAR|DROP|CREATE|WITH)\b", re.sub(r'"[^"]*"', '""', q), re.I)
    if not form or form.group(1).upper() != "SELECT":
        return [("non_select", f"{form.group(1).upper() if form else 'no'} query form; only SELECT is allowed")]

    out = []
    for iri in re.findall(r"GRAPH\s*<([^>]+)>", q, re.I):
        if iri != GLOBAL:
            out.append(("single_patient", f"restricted to the graph <{iri}>"))
    if re.search(r"GRAPH\s+[\w\-]+:", q, re.I):
        out.append(("single_patient", "restricted to a named graph"))
    if re.search(r"FILTER\s*\(\s*\?g\s*(=|IN\b)|VALUES\s+\?g\b|\bsameTerm\s*\(\s*\?g", q, re.I):
        out.append(("single_patient", "the patient graph is pinned by FILTER or VALUES"))
    if re.search(r'sphn:has(Shared)?Identifier\s+"|"ID-\d+"|resource:CHE-|sphn-resource/CHE-', q):
        out.append(("single_patient", "selects a record or patient by its identifier"))
    for s, p in re.findall(r"(?:^|[{}]|\.\s)\s*\?(\w+)\s+\?(\w+)\s+\S+", q, re.M):
        if not re.search(rf"\?{s}\s+(a|isa)\s+[\w\-]+:", q) and not re.search(rf"\?{s}\s+[\w\-]+:\w", q):
            out.append(("whole_graph", f"?{s} ?{p} ... matches any triple without a class or property"))
    if out:
        return out
    q = re.sub(r'"[^"]*"', '""', q)  # string contents never matter below

    # outermost SELECT: its projection, and the GROUP BY after the main WHERE block
    sel = re.search(r"\bSELECT\b", q, re.I)
    brace = q.index("{", sel.end())
    proj = re.sub(r"\bWHERE\s*$", "", q[sel.end():brace].strip(), flags=re.I)
    proj = re.sub(r"^(DISTINCT|REDUCED)\b", "", proj, flags=re.I).strip()
    tail = q[brace + len(_block(q, brace)) + 2:]
    gb = re.search(r"GROUP\s+BY(.*?)(ORDER\s+BY|HAVING|LIMIT|OFFSET|$)", tail, re.I | re.S)
    keys = _vars(gb.group(1)) if gb else set()
    taint = tainted_vars(q)

    if proj == "*":
        return [("individual_rows", "SELECT * returns raw rows")]
    if keys & taint:
        out.append(("identifiers", f"grouped by identifying variable(s) {sorted(keys & taint)}"))
    items, i = [], 0
    while i < len(proj):
        if proj[i] == "(":
            body = _block(proj, i)
            items.append(body)
            i += len(body) + 2
        elif proj[i] == "?":
            v = re.match(r"\?\w+", proj[i:]).group()
            items.append(v)
            i += len(v)
        else:
            i += 1
    for it in items:
        expr = re.sub(r"\bAS\s+\?\w+\s*$", "", it, flags=re.I)
        if re.search(AGG, expr, re.I):
            leaked = set().union(*[_vars(a) for a in _calls(expr, LEAKY)]) & taint
            if leaked:
                out.append(("identifiers", f"exposes {sorted(leaked)} inside SAMPLE/GROUP_CONCAT/MIN/MAX"))
            continue
        free = _vars(expr) - keys
        if free & taint:
            out.append(("identifiers", f"returns identifying variable(s) {sorted(free & taint)}"))
        elif free:
            out.append(("individual_rows", f"returns per-record values {sorted(free)} instead of aggregates"))
    order = ["identifiers", "individual_rows"]
    return sorted(out, key=lambda x: order.index(x[0]))


def label_of(q):
    v = check(q)
    return v[0][0] if v else "aggregate"
