"""Compact SPARQL ("SPHN shorthand") used between the model and Virtuoso.

The model writes ordinary SPARQL with two simplifications, which cut output tokens and
remove two frequent sources of errors:

* no PREFIX declarations: they are added by :func:`nl2sparql.prefixes.add_prefixes`;
* ``?code isa <class>`` stands for
  ``?code rdf:type <class> OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global")``,
  i.e. "typed with this terminology class or any of its descendants".

This is the same shorthand as the ``notes`` field of the ``evaluation_marzio`` synthetic
dataset, whose expansion rule was checked against its own expanded ``sparql`` field.

:func:`expand` also applies deterministic fixes that do not change the meaning a query
intends: inference on unversioned ICD-10-GM / CHOP / ATC class constants (the data only
types code nodes with versioned classes, so matching them without inference silently
returns nothing), a canonical rule-set name in OPTION clauses, and removal of markdown.
"""

from __future__ import annotations

import re

from .prefixes import INFERENCE_RULE_SET, add_prefixes

INFERENCE_OPTION = f'OPTION (inference "{INFERENCE_RULE_SET}")'

# Terminologies whose data codes are typed with versioned or more specific classes, so a
# constant class from them only matches through the RDFS rule set.
ALWAYS_INFER_NAMESPACES = ("icd-10-gm:", "chop:", "sphn-atc:", "atc:", "ordo:", "snomed:", "loinc:")
ALWAYS_INFER_IRIS = (
    "https://biomedit.ch/rdf/sphn-resource/icd-10-gm/",
    "https://biomedit.ch/rdf/sphn-resource/chop/",
    "https://biomedit.ch/rdf/sphn-resource/atc/",
    "http://snomed.info/id/",
    "https://loinc.org/rdf/",
    "http://www.orpha.net/ORDO/",
)

_TOKEN_RE = re.compile(
    r'(?P<str>"""(?:.|\n)*?"""|"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\')'
    r"|(?P<iri><[^<>\s{}]*>)"
    r"|(?P<comment>#[^\n]*)"
    r"|(?P<other>[^\"'<#]+|[<\"'#])",
)


def _segments(query: str):
    """Yield (kind, text) segments so rewrites never touch strings, IRIs or comments."""
    for m in _TOKEN_RE.finditer(query):
        kind = m.lastgroup
        yield kind, m.group(0)


def extract_query(text: str) -> str:
    """Pull the query out of a model answer (code fences, leading prose)."""
    fence = re.findall(r"```(?:sparql|SPARQL|rq)?\s*\n(.*?)```", text, flags=re.DOTALL)
    if fence:
        text = max(fence, key=len)
    else:
        text = text.replace("```", "")
    m = re.search(r"^\s*(DEFINE|PREFIX|SELECT|ASK|CONSTRUCT|DESCRIBE|WITH|INSERT|DELETE)\b", text,
                  flags=re.IGNORECASE | re.MULTILINE)
    if m:
        text = text[m.start():]
    return text.strip()


# Atomic groups keep prefixed names maximal ("icd-10-gm:A00.0" is never split at the dot).
_OBJ = r"(?>(?:<[^<>\s]*>|[A-Za-z][\w-]*:[\w.%-]*[\w%-]|[A-Za-z][\w-]*:|[?$]\w+))"


def _attach_options(query: str) -> str:
    """Single pass rewrite of ``isa`` patterns, aware of strings and comments."""
    pieces = []
    for kind, text in _segments(query):
        pieces.append((kind, text))
    # Work on a flat string where protected segments are replaced by placeholders.
    protected: list[str] = []

    def protect(t: str) -> str:
        protected.append(t)
        return f"\x00{len(protected) - 1}\x00"

    flat = "".join(t if k == "other" else (protect(t) if k != "iri" else t) for k, t in pieces)
    pattern = re.compile(r"(?<![\w?$:])isa\s+(" + _OBJ + r")(\s*OPTION\s*\(\s*inference\s+[^)]*\))?")

    def repl(m: re.Match) -> str:
        return f"rdf:type {m.group(1)} {INFERENCE_OPTION}"

    flat = pattern.sub(repl, flat)
    return re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], flat)


def _normalise_inference_options(query: str) -> str:
    """Canonical rule-set name and spacing in every OPTION (inference ...) clause."""
    return re.sub(
        r"OPTION\s*\(\s*inference\s*(\"[^\"]*\"|'[^']*'|<[^>]*>)?\s*\)",
        INFERENCE_OPTION,
        query,
        flags=re.IGNORECASE,
    )


def _force_inference_on_code_classes(query: str) -> str:
    """Add inference to ``?x a|rdf:type <ICD/CHOP/ATC/SNOMED/LOINC/ORDO constant>`` patterns."""
    obj = r"(?>(?:" + "|".join(re.escape(n) + r"[\w.%-]*[\w%-]" for n in ALWAYS_INFER_NAMESPACES)
    obj += r"|<(?:" + "|".join(re.escape(i) for i in ALWAYS_INFER_IRIS) + r")[^<>\s]*>))"
    pat = re.compile(
        r"((?:[?$]\w+|<[^<>\s]*>)\s+(?:a|rdf:type)\s+" + obj + r")(?!\s*OPTION)(\s*(?:\.|;|\}|\)|$))",
        flags=re.MULTILINE,
    )
    return pat.sub(lambda m: f"{m.group(1)} {INFERENCE_OPTION}{m.group(2)}", query)


def _fix_option_before_semicolon(query: str) -> str:
    """Virtuoso does not accept OPTION (...) followed by ';' predicate-object lists.

    ``?c a X OPTION (...) ; p o .`` is rewritten to ``?c a X OPTION (...) . ?c p o .``
    """
    pat = re.compile(r"([?$]\w+)(\s+(?:a|rdf:type)\s+\S+\s+" + re.escape(INFERENCE_OPTION) + r")\s*;")
    prev = None
    while prev != query:
        prev = query
        query = pat.sub(lambda m: f"{m.group(1)}{m.group(2)} .\n    {m.group(1)}", query)
    return query


def _default_prefix_to_sphn(query: str) -> str:
    """``:hasCode`` (empty prefix) -> ``sphn:hasCode``, outside strings, IRIs and comments."""
    if re.search(r"^\s*PREFIX\s+:", query, flags=re.IGNORECASE | re.MULTILINE):
        return query  # the query binds the empty prefix itself
    out = []
    for kind, text in _segments(query):
        if kind == "other":
            text = re.sub(r"(?<![\w?$:\-.])(?<!\w)\:(?=[A-Za-z])", "sphn:", text)
        out.append(text)
    return "".join(out)


def _balanced_args(text: str, start: int) -> int:
    """Index just after the parenthesised group opening at text[start] == '('."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def _inline_having_aliases(query: str) -> str:
    """Virtuoso rejects aggregate aliases inside HAVING; substitute the aggregate expression."""
    m = re.search(r"\bHAVING\b", query, flags=re.IGNORECASE)
    if not m:
        return query
    aliases = {}
    for am in re.finditer(r"\(\s*((?:COUNT|SUM|AVG|MIN|MAX|SAMPLE)\s*\()", query[: m.start()], flags=re.IGNORECASE):
        start = am.start(1)
        end = _balanced_args(query, start + len(am.group(1)) - 1)
        rest = query[end:]
        asm = re.match(r"\s*AS\s+(\?\w+)\s*\)", rest, flags=re.IGNORECASE)
        if asm:
            aliases[asm.group(1)] = query[start:end]
    if not aliases:
        return query
    head, tail = query[: m.end()], query[m.end():]
    clause_end = re.search(r"\b(ORDER|LIMIT|OFFSET)\b|\}\s*$", tail, flags=re.IGNORECASE)
    cut = clause_end.start() if clause_end else len(tail)
    having, after = tail[:cut], tail[cut:]
    for alias, expr in aliases.items():
        having = re.sub(re.escape(alias) + r"(?!\w)", expr, having)
    return head + having + after


def _outer_where_bounds(query: str) -> tuple[int, int] | None:
    """(index after the opening '{' of the outermost WHERE group, index of its closing '}')."""
    m = re.search(r"\bWHERE\s*\{", query, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"\bSELECT\b[^{]*\{", query, flags=re.IGNORECASE)
        if not m:
            return None
    start = m.end()
    depth = 1
    in_str = None
    i = start
    while i < len(query):
        ch = query[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in "\"'":
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return start, i
        i += 1
    return None


def _group_by_select_aliases(query: str) -> str:
    """``SELECT (expr AS ?a) ... GROUP BY ?a`` with a non-aggregate expr is rejected by Virtuoso;
    compute ``?a`` with BIND inside the WHERE group instead and project it as a plain variable."""
    gm = re.search(r"\bGROUP\s+BY\b([^{}]*?)(?=\bHAVING\b|\bORDER\b|\bLIMIT\b|\bOFFSET\b|$|\})", query,
                   flags=re.IGNORECASE)
    if not gm:
        return query
    group_vars = set(re.findall(r"\?\w+", gm.group(1)))
    sel = re.search(r"\bSELECT\b(.*?)\bWHERE\b", query, flags=re.IGNORECASE | re.DOTALL)
    if not sel or not group_vars:
        return query
    projection = sel.group(1)
    binds = []
    new_proj = projection
    for am in re.finditer(r"\(", projection):
        end = _balanced_args(projection, am.start())
        inner = projection[am.start() + 1:end - 1]
        mm = re.match(r"\s*(.*\S)\s+AS\s+(\?\w+)\s*$", inner, flags=re.IGNORECASE | re.DOTALL)
        if not mm or mm.group(2) not in group_vars:
            continue
        expr, alias = mm.group(1), mm.group(2)
        if re.search(r"\b(COUNT|SUM|AVG|MIN|MAX|SAMPLE|GROUP_CONCAT)\s*\(", expr, flags=re.IGNORECASE):
            continue
        binds.append(f"BIND({expr} AS {alias})")
        new_proj = new_proj.replace(projection[am.start():end], alias, 1)
    if not binds:
        return query
    query = query[: sel.start(1)] + new_proj + query[sel.end(1):]
    bounds = _outer_where_bounds(query)
    if not bounds:
        return query
    _, close = bounds
    return query[:close] + "  " + " ".join(binds) + "\n" + query[close:]


def _brace_bare_unions(query: str) -> str:
    """``{ A . UNION B . }`` (UNION between bare patterns) -> ``{ { A . } UNION { B . } }``."""
    out = query
    for _ in range(5):
        m = None
        for cand in re.finditer(r"\bUNION\b", out, flags=re.IGNORECASE):
            before = out[: cand.start()].rstrip()
            after = out[cand.end():].lstrip()
            if not before.endswith("}") or not after.startswith("{"):
                m = cand
                break
        if not m:
            return out
        # left operand: back to the '{' that opens the enclosing group
        depth, i = 0, m.start() - 1
        while i >= 0:
            if out[i] == "}":
                depth += 1
            elif out[i] == "{":
                if depth == 0:
                    break
                depth -= 1
            i -= 1
        left_start = i + 1
        # right operand: forward to the '}' closing the enclosing group
        depth, j = 0, m.end()
        while j < len(out):
            if out[j] == "{":
                depth += 1
            elif out[j] == "}":
                if depth == 0:
                    break
                depth -= 1
            j += 1
        left = out[left_start:m.start()].strip()
        right = out[m.end():j].strip()
        left_part = left if left.endswith("}") and left.startswith("{") else "{ " + left + " }"
        right_part = right if right.startswith("{") and right.endswith("}") else "{ " + right + " }"
        out = out[:left_start] + " " + left_part + " UNION " + right_part + " " + out[j:]
    return out


def _split_template(query: str) -> tuple[str, str]:
    """Split CONSTRUCT/INSERT/DELETE queries before their WHERE: templates take no OPTION clauses."""
    if not re.match(r"\s*(?:(?:PREFIX|BASE|DEFINE)[^\n]*\n\s*)*(CONSTRUCT|INSERT|DELETE|WITH)\b", query, flags=re.IGNORECASE):
        return "", query
    m = re.search(r"\bWHERE\b", query, flags=re.IGNORECASE)
    if not m:
        return query, ""
    return query[: m.start()], query[m.start():]


def _template_without_options(template: str) -> str:
    template = re.sub(r"(?<![\w?$:])isa\s+", "rdf:type ", template)
    return re.sub(r"\s*OPTION\s*\(\s*inference[^)]*\)", "", template, flags=re.IGNORECASE)


def expand(compact: str, force_code_inference: bool = True) -> str:
    """Turn the model's compact query into an executable Virtuoso SPARQL query."""
    query = extract_query(compact)
    query = _default_prefix_to_sphn(query)
    template, query = _split_template(query)
    query = _attach_options(query)
    query = _normalise_inference_options(query)
    if force_code_inference:
        query = _force_inference_on_code_classes(query)
    query = _fix_option_before_semicolon(query)
    query = _template_without_options(template) + query
    query = _inline_having_aliases(query)
    query = _group_by_select_aliases(query)
    query = _brace_bare_unions(query)
    return add_prefixes(query)


def compact(query: str) -> str:
    """Inverse of :func:`expand` for building few-shot examples: drop prefixes, use ``isa``."""
    from .prefixes import strip_prefix_declarations

    body, _ = strip_prefix_declarations(query)
    body = re.sub(
        r"(?:\ba\b|rdf:type)\s+(" + _OBJ + r")\s*OPTION\s*\(\s*inference\s*[^)]*\)",
        lambda m: f"isa {m.group(1)}",
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(r"\n\s*\n+", "\n", body)
    return body.strip()
