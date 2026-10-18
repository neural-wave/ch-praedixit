"""Virtuoso extensions to SPARQL: `DEFINE` pragmas and `OPTION (...)` clauses.

Standard SPARQL parsers reject both, so they are checked against an allow list here and
blanked out of the text before parsing. Allowed:
- `DEFINE sql:select-option "order"`, which makes Virtuoso join the triple patterns in the
  written order;
- `OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global")`, which resolves one
  triple pattern through the RDFS rule set of the SPHN terminologies.
Any other extension is reported, because its effect cannot be verified: for example
`DEFINE get:soft "replace"` makes the server download a remote graph and store it.

Virtuoso also predefines some prefixes (bif:, rdfs:, xsd:...), so a query may use them without
declaring them; the missing declarations are added before parsing.
"""

import dataclasses
import re

from pppp.checker.GateB.rules import Rule, Violation

ALLOWED_DEFINES = {("sql:select-option", '"order"')}
ALLOWED_INFERENCE_RULE_SETS = {"https://biomedit.ch/rdf/sphn-resource/global"}

# String literals, IRIs and comments: their content must not be read as SPARQL keywords.
OPAQUE_SPAN = re.compile(
    r'"(?:[^"\\\n]|\\.)*"'
    r"|'(?:[^'\\\n]|\\.)*'"
    r'|<[^<>"{}|^`\\\s]*>'
    r"|#[^\n]*"
)
# The value group starts right after one blank: in the masked text the value itself can be
# blanks, which a greedy separator would swallow.
DEFINE_LINE = re.compile(r"^[ \t]*DEFINE[ \t]+(\S+)[ \t](.*)$", re.MULTILINE | re.IGNORECASE)
OPTION_CLAUSE = re.compile(r"\bOPTION\s*\(([^()]*)\)", re.IGNORECASE)
INFERENCE_ARGUMENT = re.compile(r'^\s*inference\s+"([^"]*)"\s*$', re.IGNORECASE)
# Prefixes Virtuoso declares itself, so queries may use them undeclared.
VIRTUOSO_PREFIXES = {
    "bif": "bif:",
    "sql": "sql:",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "foaf": "http://xmlns.com/foaf/0.1/",
}
PREFIX_DECLARATION = re.compile(r"\bPREFIX\s+([A-Za-z][\w.-]*)?\s*:", re.IGNORECASE)
# `name:` used in a prefixed name (not inside a variable name, an IRI or another prefixed name).
PREFIX_USE = re.compile(r"(?<![\w?$:/.-])([A-Za-z][\w.-]*):")


@dataclasses.dataclass
class ExtensionScan:
    """Result of scanning a query for Virtuoso extensions.

    standard_text: the query with its DEFINE lines and OPTION clauses replaced by spaces,
        so that a standard parser accepts it and reports error positions of the original text.
    violations: one Violation per extension that is not allowed.
    """

    standard_text: str
    violations: list


def mask_opaque_spans(query_text):
    """Return `query_text` with string literals, IRIs and comments replaced by spaces.

    Line breaks are kept, so the masked text has the same length and line numbers.
    """
    def blank_out(span_match):
        return re.sub(r"[^\n]", " ", span_match.group(0))

    return OPAQUE_SPAN.sub(blank_out, query_text)


def blank_out_spans(text, spans):
    """Return `text` with each (start, end) span replaced by spaces."""
    characters = list(text)
    for start, end in spans:
        for position in range(start, end):
            if characters[position] != "\n":
                characters[position] = " "
    return "".join(characters)


def scan_virtuoso_extensions(query_text):
    """Find the DEFINE pragmas and OPTION clauses of a query and check them.

    Returns an ExtensionScan. Keywords are searched in the masked text, so a DEFINE or an
    OPTION written inside a string, an IRI or a comment is ignored; the values are read
    from the original text.
    """
    masked_text = mask_opaque_spans(query_text)
    extension_spans = []
    violations = []

    for define_match in DEFINE_LINE.finditer(masked_text):
        pragma_name = query_text[define_match.start(1):define_match.end(1)]
        # The value is usually a string, blank in the masked text: read it from the original.
        pragma_value = query_text[define_match.start(2):define_match.end(2)].strip()
        extension_spans.append(define_match.span())
        if (pragma_name, pragma_value) not in ALLOWED_DEFINES:
            reason = f"Virtuoso pragma 'DEFINE {pragma_name} {pragma_value}' is not allowed: its effect cannot be verified."
            violations.append(Violation(Rule.NOT_READ_ONLY, reason))

    for option_match in OPTION_CLAUSE.finditer(masked_text):
        option_argument = query_text[option_match.start(1):option_match.end(1)].strip()
        extension_spans.append(option_match.span())
        inference_match = INFERENCE_ARGUMENT.match(option_argument)
        if inference_match is None:
            reason = f"Virtuoso clause 'OPTION ({option_argument})' is not allowed: its effect cannot be verified."
            violations.append(Violation(Rule.NOT_READ_ONLY, reason))
            continue
        rule_set = inference_match.group(1)
        if rule_set not in ALLOWED_INFERENCE_RULE_SETS:
            reason = f"Inference rule set '{rule_set}' is not allowed."
            violations.append(Violation(Rule.NOT_READ_ONLY, reason))

    standard_text = blank_out_spans(query_text, extension_spans)
    standard_text = declare_virtuoso_prefixes(standard_text)
    return ExtensionScan(standard_text=standard_text, violations=violations)


def declare_virtuoso_prefixes(standard_text):
    """Add declarations for the Virtuoso predefined prefixes the query uses without declaring them.

    They are put in front of the first line, so error line numbers stay those of the original text.
    """
    masked_text = mask_opaque_spans(standard_text)
    declared_prefixes = set()
    for declaration_match in PREFIX_DECLARATION.finditer(masked_text):
        declared_prefixes.add((declaration_match.group(1) or "").lower())
    declarations = []
    used_prefixes = sorted(set(PREFIX_USE.findall(masked_text)))
    for prefix in used_prefixes:
        if prefix.lower() in VIRTUOSO_PREFIXES and prefix.lower() not in declared_prefixes:
            declarations.append(f"PREFIX {prefix}: <{VIRTUOSO_PREFIXES[prefix.lower()]}> ")
    return "".join(declarations) + standard_text
