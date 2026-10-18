"""Intent rules: what Gate A predicts from the wording of a request.

Gate A runs before the SPARQL query is generated, so it judges the request text alone. The
intent rule of a request is the compliance rule its wording asks to break, or "none" when the
request itself is acceptable. The names are those of the Rule enum of GateB/rules.py.

The labels in data/ judge the query, not the request, so the intent rule is derived from them:
- an `innocent_request` entry pairs a harmless request with a leaking query: intent "none";
- `invalid_sparql` is an error of the query only: intent "none";
- `unrestricted_match` depends on the shape of the query and the wording does not tell
  whether it is acceptable, so these entries have no intent rule and are skipped.
"""

INTENT_RULES = ["none", "exposes_identifier", "not_aggregated", "single_patient", "not_read_only", "not_select"]


def generated_intent_rule(entry):
    """Return the intent rule of a data/generated.jsonl entry, or None when the entry is skipped."""
    query_rule = entry["expected_rule"]
    if query_rule == "unrestricted_match":
        return None
    if entry["request_kind"] == "innocent_request":
        return "none"
    if query_rule == "invalid_sparql":
        return "none"
    return query_rule


def rule_name(rule_text):
    """Return the rule name of one `expected_rules` item.

    Items are either a bare name ("single_patient") or a code followed by the name in
    parentheses ("QC1 (single_patient)", the format of data/test-queries/).
    """
    rule_text = rule_text.strip()
    if rule_text.endswith(")") and "(" in rule_text:
        return rule_text[rule_text.index("(") + 1:-1].strip()
    return rule_text


def evaluation_intent_rule(header):
    """Return the intent rule of an evaluation set entry from its header, or None when it is skipped.

    Ambiguous entries are skipped. Only the first rule of `expected_rules` is used: it is the
    main rule of the entry.
    """
    expected_compliant = header["expected_compliant"]
    if expected_compliant == "true":
        return "none"
    if expected_compliant != "false":
        return None
    main_rule = rule_name(header["expected_rules"].split(",")[0])
    if main_rule not in INTENT_RULES:
        return None
    return main_rule
