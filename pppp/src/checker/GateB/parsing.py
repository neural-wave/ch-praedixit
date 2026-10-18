"""Parsing of a request whose Virtuoso extensions have been blanked out.

A request is a query (SELECT, ASK, CONSTRUCT, DESCRIBE), which rdflib turns into the SPARQL
algebra, an update (INSERT, DELETE, LOAD, CLEAR, DROP...), or invalid.

rdflib keeps the patterns of `FILTER (NOT) EXISTS { ... }` in parse form until it evaluates the
query; they are translated here so that the checks see their triple patterns and filters.
"""

import dataclasses
import re

from rdflib.plugins.sparql import prepareQuery
from rdflib.plugins.sparql.algebra import translateGroupGraphPattern
from rdflib.plugins.sparql.parser import parseUpdate
from rdflib.plugins.sparql.parserutils import CompValue

from pppp.checker.GateB.virtuoso_extensions import mask_opaque_spans

UPDATE_KEYWORD = re.compile(
    r"(?<![?$:\w-])(INSERT|DELETE|LOAD|CLEAR|DROP|CREATE|ADD|MOVE|COPY|MODIFY)\b",
    re.IGNORECASE,
)


@dataclasses.dataclass
class ParsedRequest:
    """Outcome of parsing a request.

    algebra: the root algebra node (`SelectQuery`, `AskQuery`, ...) when the request is a query.
    is_update: True when the request modifies data.
    error: the first line of the parser error when the request is neither.
    """

    algebra: object = None
    is_update: bool = False
    error: str = None


def translate_exists_patterns(node):
    """Translate in place the group patterns of the EXISTS and NOT EXISTS nodes below `node`."""
    if isinstance(node, CompValue):
        if node.name in ("Builtin_EXISTS", "Builtin_NOTEXISTS"):
            # CompValue.get resolves attribute values; dict.get reads the stored pattern itself.
            pattern = dict.get(node, "graph")
            if isinstance(pattern, CompValue) and pattern.name.startswith("GroupGraphPattern"):
                dict.__setitem__(node, "graph", translateGroupGraphPattern(pattern))
        for field_value in dict.values(node):
            translate_exists_patterns(field_value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            translate_exists_patterns(item)


def parse_request(standard_text):
    """Parse a request that contains no Virtuoso extensions; return a ParsedRequest."""
    # rdflib raises several exception types (pyparsing errors, translation errors) for invalid input.
    try:
        algebra = prepareQuery(standard_text).algebra
        translate_exists_patterns(algebra)
        return ParsedRequest(algebra=algebra)
    except Exception as query_error:
        query_error_line = str(query_error).splitlines()[0]

    try:
        parseUpdate(standard_text)
        return ParsedRequest(is_update=True)
    except Exception:
        pass

    # Virtuoso accepts update forms that rdflib does not know (e.g. MODIFY); an update
    # keyword in a request that is not a valid query is enough to treat it as an update.
    if UPDATE_KEYWORD.search(mask_opaque_spans(standard_text)):
        return ParsedRequest(is_update=True)
    return ParsedRequest(error=query_error_line)
