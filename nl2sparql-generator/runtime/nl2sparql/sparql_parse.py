"""Lightweight SPARQL inspection: tokenizer, triple-pattern extraction, structural facts.

Virtuoso syntax (DEFINE, OPTION (inference ...), bif: functions) is not accepted by
standard SPARQL parsers such as rdflib, which is why sparql-llm's validator cannot be used
as is on SPHN queries. Syntax itself is checked by Virtuoso (the target engine); this module
only extracts what the schema and semantic checks need, and is tolerant of what it does not
understand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<comment>\#[^\n]*)
  | (?P<string>\"\"\"(?:.|\n)*?\"\"\"|'''(?:.|\n)*?'''|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
  | (?P<iri><[^<>"{}|^`\\\s]*>)
  | (?P<var>[?$][A-Za-z_][\w]*)
  | (?P<number>[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
  | (?P<pname>(?:[A-Za-z][\w-]*(?:\.[\w-]+)*)?:(?:[\w%\\-](?:[\w.%\\-]*[\w%\\-])?)?)
  | (?P<lang>@[A-Za-z]+(?:-[A-Za-z0-9]+)*)
  | (?P<op>\^\^|&&|\|\||!=|<=|>=|[{}()\[\].,;*/|^!=<>+\-?])
  | (?P<word>[A-Za-z_][\w]*)
  | (?P<other>.)
    """,
    re.VERBOSE,
)


@dataclass
class Token:
    kind: str
    text: str
    pos: int


def tokenize(query: str) -> list[Token]:
    toks = []
    for m in TOKEN_RE.finditer(query):
        kind = m.lastgroup
        if kind in ("ws", "comment"):
            continue
        toks.append(Token(kind, m.group(0), m.start()))
    return toks


@dataclass
class Triple:
    s: str
    p: str  # predicate or property path text, 'a' normalised to rdf:type
    o: str
    graph: str | None
    optional_depth: int
    inference: bool = False


@dataclass
class QueryFacts:
    form: str = "UNKNOWN"
    triples: list[Triple] = field(default_factory=list)
    projected: list[str] = field(default_factory=list)
    has_group_by: bool = False
    aggregates: list[str] = field(default_factory=list)
    keywords: set[str] = field(default_factory=set)
    variables: set[str] = field(default_factory=set)
    brace_balance: int = 0
    paren_balance: int = 0


TERM_KINDS = ("var", "iri", "pname", "string", "number")


def _skip_group(toks: list[Token], i: int, open_t: str, close_t: str) -> int:
    depth = 0
    while i < len(toks):
        if toks[i].text == open_t:
            depth += 1
        elif toks[i].text == close_t:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def _read_term(toks: list[Token], i: int) -> tuple[str | None, int]:
    if i >= len(toks):
        return None, i
    t = toks[i]
    if t.kind in TERM_KINDS:
        j = i + 1
        # typed literal / language tag
        if t.kind == "string" and j < len(toks) and toks[j].text == "^^":
            j += 2
        elif t.kind == "string" and j < len(toks) and toks[j].kind == "lang":
            j += 1
        return t.text, j
    if t.text == "[":
        return "[]", _skip_group(toks, i, "[", "]")
    if t.text == "(":
        return "()", _skip_group(toks, i, "(", ")")
    if t.kind == "word" and t.text.lower() in ("true", "false"):
        return t.text, i + 1
    return None, i


def _read_path(toks: list[Token], i: int) -> tuple[str | None, int]:
    """Predicate or property path: a | iri | pname | var | (path) with / | ^ * + ? operators."""
    parts = []
    j = i
    expect_elem = True
    while j < len(toks):
        t = toks[j]
        if expect_elem:
            if t.kind == "word" and t.text == "a":
                parts.append("rdf:type")
            elif t.kind in ("iri", "pname", "var"):
                parts.append(t.text)
            elif t.text == "^" or t.text == "!":
                parts.append(t.text)
                j += 1
                continue
            elif t.text == "(":
                end = _skip_group(toks, j, "(", ")")
                parts.append("(" + " ".join(x.text for x in toks[j + 1:end - 1]) + ")")
                j = end
                expect_elem = False
                continue
            else:
                break
            j += 1
            expect_elem = False
        else:
            if t.text in ("/", "|"):
                parts.append(t.text)
                j += 1
                expect_elem = True
            elif t.text in ("*", "+", "?") and t.kind == "op":
                parts.append(t.text)
                j += 1
            else:
                break
    if not parts:
        return None, i
    return "".join(parts), j


def analyse(query: str) -> QueryFacts:
    toks = tokenize(query)
    facts = QueryFacts()
    facts.brace_balance = sum(1 for t in toks if t.text == "{") - sum(1 for t in toks if t.text == "}")
    facts.paren_balance = sum(1 for t in toks if t.text == "(") - sum(1 for t in toks if t.text == ")")
    for t in toks:
        if t.kind == "word":
            facts.keywords.add(t.text.upper())
        elif t.kind == "var":
            facts.variables.add(t.text[1:])
    for t in toks:
        if t.kind == "word" and t.text.upper() in ("SELECT", "ASK", "CONSTRUCT", "DESCRIBE", "INSERT", "DELETE",
                                                   "LOAD", "CLEAR", "DROP", "CREATE"):
            facts.form = t.text.upper()
            break
    facts.has_group_by = any(
        toks[k].text.upper() == "GROUP" and k + 1 < len(toks) and toks[k + 1].text.upper() == "BY"
        for k in range(len(toks))
    )
    facts.aggregates = [t.text.upper() for t in toks if t.kind == "word" and t.text.upper() in
                        ("COUNT", "SUM", "AVG", "MIN", "MAX", "SAMPLE", "GROUP_CONCAT")]
    # Projection of the outermost SELECT
    for k, t in enumerate(toks):
        if t.kind == "word" and t.text.upper() == "SELECT":
            j = k + 1
            while j < len(toks) and not (toks[j].kind == "word" and toks[j].text.upper() in ("WHERE", "FROM")) \
                    and toks[j].text != "{":
                if toks[j].kind == "var":
                    # "(... AS ?x)" projects ?x; a bare ?v projects ?v
                    prev = toks[j - 1].text.upper() if j > 0 else ""
                    if prev == "AS" or (toks[j - 1].text not in ("(",) and prev != "AS" and _depth(toks, k + 1, j) == 0):
                        facts.projected.append(toks[j].text)
                elif toks[j].text == "*" and _depth(toks, k + 1, j) == 0:
                    facts.projected.append("*")
                j += 1
            break
    facts.triples = _extract_triples(toks)
    return facts


def _depth(toks: list[Token], start: int, end: int) -> int:
    d = 0
    for t in toks[start:end]:
        if t.text == "(":
            d += 1
        elif t.text == ")":
            d -= 1
    return d


def _extract_triples(toks: list[Token]) -> list[Triple]:
    triples: list[Triple] = []
    graph_stack: list[tuple[int, str | None]] = []
    optional_stack: list[int] = []
    depth = 0
    i = 0
    pending_graph: str | None = None
    pending_optional = False
    current_graph: str | None = None

    def cur_graph() -> str | None:
        return graph_stack[-1][1] if graph_stack else None

    while i < len(toks):
        t = toks[i]
        up = t.text.upper()
        if t.kind == "word" and up == "GRAPH" and i + 1 < len(toks):
            pending_graph = toks[i + 1].text
            i += 2
            continue
        if t.kind == "word" and up == "OPTIONAL":
            pending_optional = True
            i += 1
            continue
        if t.kind == "word" and up in ("FILTER", "BIND", "HAVING", "VALUES", "ORDER", "GROUP", "LIMIT", "OFFSET",
                                       "SELECT", "OPTION", "DEFINE", "PREFIX", "BASE"):
            # Skip expressions: FILTER(...)/BIND(...)/OPTION(...). FILTER [NOT] EXISTS { } is descended into.
            j = i + 1
            if up == "FILTER" and j < len(toks) and toks[j].kind == "word" and toks[j].text.upper() in ("NOT", "EXISTS"):
                i = j
                continue
            if up == "VALUES":
                # VALUES ?x { ... } or VALUES (?a ?b) { ... }
                while j < len(toks) and toks[j].text != "{":
                    j += 1
                i = _skip_group(toks, j, "{", "}")
                continue
            if up == "SELECT":
                while j < len(toks) and toks[j].text != "{" and not (toks[j].kind == "word" and toks[j].text.upper() == "WHERE"):
                    j += 1
                i = j
                continue
            if up in ("ORDER", "GROUP"):
                j += 1  # BY
                while j < len(toks) and (toks[j].kind in ("var",) or toks[j].text == "(" or (toks[j].kind == "word" and toks[j].text.upper() in ("ASC", "DESC"))):
                    if toks[j].text == "(":
                        j = _skip_group(toks, j, "(", ")")
                    else:
                        j += 1
                i = j
                continue
            if up in ("LIMIT", "OFFSET"):
                i = j + 1
                continue
            if up == "DEFINE":
                i = j + 2
                continue
            if up in ("PREFIX", "BASE"):
                i = j + 2 if up == "PREFIX" else j + 1
                continue
            if j < len(toks) and toks[j].text == "(":
                end = _skip_group(toks, j, "(", ")")
                triples.extend(_inner_group_triples(toks[j:end], len(optional_stack), cur_graph()))
                i = end
            else:
                # FILTER regex(...) or FILTER bound(...) without outer parens
                k = j + 1
                if k < len(toks) and toks[k].text == "(":
                    i = _skip_group(toks, k, "(", ")")
                else:
                    i = j
            continue
        if t.kind == "word" and up in ("EXISTS", "NOT", "UNION", "MINUS", "WHERE", "SERVICE", "SILENT"):
            i += 1
            continue
        if t.text == "{":
            depth += 1
            if pending_graph is not None:
                graph_stack.append((depth, pending_graph))
                pending_graph = None
            if pending_optional:
                optional_stack.append(depth)
                pending_optional = False
            i += 1
            continue
        if t.text == "}":
            if graph_stack and graph_stack[-1][0] == depth:
                graph_stack.pop()
            if optional_stack and optional_stack[-1] == depth:
                optional_stack.pop()
            depth -= 1
            i += 1
            continue
        if t.text in (".", ";", ","):
            i += 1
            continue
        # Try to read a triples block starting at a subject
        subj, j = _read_term(toks, i)
        if subj is None:
            i += 1
            continue
        while True:
            pred, k = _read_path(toks, j)
            if pred is None:
                i = max(j, i + 1)
                break
            while True:
                obj, m = _read_term(toks, k)
                if obj is None:
                    break
                inference = False
                if m < len(toks) and toks[m].kind == "word" and toks[m].text.upper() == "OPTION":
                    seg_end = _skip_group(toks, m + 1, "(", ")")
                    if any(x.text.lower() == "inference" for x in toks[m + 1:seg_end]):
                        inference = True
                    m = seg_end
                triples.append(Triple(subj, pred, obj, cur_graph(), len(optional_stack), inference))
                k = m
                if k < len(toks) and toks[k].text == ",":
                    k += 1
                    continue
                break
            if k < len(toks) and toks[k].text == ";":
                k += 1
                while k < len(toks) and toks[k].text == ";":
                    k += 1
                j = k
                # a dangling ';' before '.' or '}'
                if k < len(toks) and toks[k].text in (".", "}"):
                    i = k
                    break
                continue
            i = k
            break
        current_graph = None
    del current_graph
    return triples


def _inner_group_triples(toks: list[Token], optional_depth: int, graph: str | None) -> list[Triple]:
    """Triples of EXISTS { ... } groups nested inside FILTER/BIND expressions."""
    out: list[Triple] = []
    k = 0
    while k < len(toks):
        if toks[k].text == "{":
            end = _skip_group(toks, k, "{", "}")
            for t in _extract_triples(toks[k:end]):
                out.append(Triple(t.s, t.p, t.o, t.graph or graph, optional_depth + 1 + t.optional_depth, t.inference))
            k = end
        else:
            k += 1
    return out


def components(triples: list[Triple]) -> list[set[str]]:
    """Connected components of variables linked by triple patterns (for Cartesian-product checks)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for t in triples:
        vs = [x for x in (t.s, t.o) if x.startswith("?")]
        if t.graph and t.graph.startswith("?"):
            vs.append(t.graph)
        for v in vs:
            find(v)
        for a, b in zip(vs, vs[1:]):
            union(a, b)
    groups: dict[str, set[str]] = {}
    for v in parent:
        groups.setdefault(find(v), set()).add(v)
    return list(groups.values())
