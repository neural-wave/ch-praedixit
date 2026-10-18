"""Final compliance verdict of a request, from Gate A (the request text) and Gate B (the query).

Gate A judges the wording of the request before any SPARQL is generated; Gate B checks the
generated query against the rules (RULES.md). Every rule is one Gate B checks in the query, so
Gate B has the last word:

    Gate A    Gate B    final verdict
    passes    passes    compliant
    refuses   rejects   non-compliant (they agree)
    passes    rejects   non-compliant; retry the translation first (it may be a translator error)
    refuses   passes    compliant: Gate B overturns Gate A's refusal

A query that does not parse, or that uses classes or properties the patient data does not contain
(schema_check), is always translated again, whatever Gate A said: it is a translator mistake, and
retry_feedback tells the translator what to fix. Schema problems never change the verdict.

A Gate A refusal therefore never ends the request on its own: the request is still translated
(the API returns a query for every request) and Gate B decides. Gate A decides how the request is
handled: whether a Gate B rejection is worth a retry, and the reason given when only it objects.

`gate_a_threshold` can let a very confident Gate A refusal stand against a Gate B pass, for
requests that single out a patient by intent alone ("the BMI of the patient with ...").
It is off by default: Gate B always overturns.
"""

import dataclasses

from pppp.checker.GateB.rules import Rule

# Probability above which a Gate A refusal stands against a Gate B pass; None: never.
DEFAULT_GATE_A_THRESHOLD = None
# Translations retried when Gate B rejects a query whose request Gate A accepted.
DEFAULT_MAX_RETRIES = 1


@dataclasses.dataclass(frozen=True)
class FinalVerdict:
    """The verdict returned by the API for one request.

    is_compliant: the `is_compliant` field of the response.
    reason: the `compliance_reason` field, None when the request is compliant.
    decided_by: "both" when the gates agree, "gate_b" when Gate B overturns or rejects alone,
        "gate_a" when a Gate A refusal stands (only with a gate_a_threshold).
    """

    is_compliant: bool
    reason: str = None
    decided_by: str = "both"


def gate_a_refused(gate_a_decision):
    """Return True if Gate A refused the request; a missing decision (Gate A not run) refuses nothing."""
    return gate_a_decision is not None and gate_a_decision.refused


def final_verdict(gate_a_decision, gate_b_violations, gate_a_threshold=DEFAULT_GATE_A_THRESHOLD):
    """Return the FinalVerdict of a request.

    `gate_a_decision` is a GateA GateADecision (or None when Gate A did not run);
    `gate_b_violations` is the list of Violation that Gate B reported for the final query.
    """
    refused = gate_a_refused(gate_a_decision)
    if gate_b_violations:
        reason = " ".join(dict.fromkeys(violation.reason for violation in gate_b_violations))
        return FinalVerdict(is_compliant=False, reason=reason, decided_by="both" if refused else "gate_b")
    if not refused:
        return FinalVerdict(is_compliant=True, decided_by="both")
    confident = gate_a_threshold is not None and gate_a_decision.non_compliant_probability >= gate_a_threshold
    if confident:
        reason = (f"The request asks to break the rule '{gate_a_decision.rule}' "
                  f"(probability {gate_a_decision.non_compliant_probability:.2f}), although the generated "
                  "query passes the structural checks.")
        return FinalVerdict(is_compliant=False, reason=reason, decided_by="gate_a")
    return FinalVerdict(is_compliant=True, decided_by="gate_b")


def is_invalid(gate_b_violations):
    """Return True if Gate B could not parse the query (it then ran none of its other checks)."""
    return any(violation.rule == Rule.INVALID_SPARQL for violation in gate_b_violations)


def should_retry_translation(gate_a_decision, gate_b_violations, retries_done, max_retries=DEFAULT_MAX_RETRIES,
                             schema_problems=()):
    """Return True if the request should be translated again.

    - The query does not parse, or uses classes or properties the data does not contain
      (`schema_problems`, see schema_check): always, whatever Gate A said. These are mistakes of
      the translator, never of the request, and an unparsed query cannot be checked by Gate B.
    - Gate B rejects a query whose request Gate A accepted: the request looks fine, so the
      translation is the likely culprit.
    - Otherwise not: when both gates object, the request itself is the problem and a retry would
      only cost time and tokens.

    A retry must translate the same request: it must not rewrite a non-compliant request into a
    compliant one (see retry_feedback).
    """
    if retries_done >= max_retries:
        return False
    if is_invalid(gate_b_violations) or schema_problems:
        return True
    return bool(gate_b_violations) and not gate_a_refused(gate_a_decision)


def retry_feedback(gate_b_violations, schema_problems=()):
    """Return the text to give the translator for its next attempt, or "" when there is nothing to say.

    The translator is deterministic (temperature 0): without feedback a retry writes the same query.
    Parser errors and schema problems are reported in full, so that it can correct them. A
    compliance rejection is only named, with the instruction to keep translating literally: asking
    the translator to remove what Gate B objects to would turn a non-compliant request into a
    compliant query, and the verdict would then be wrong.
    """
    lines = []
    for violation in gate_b_violations:
        if violation.rule == Rule.INVALID_SPARQL:
            lines.append(f"Your previous query is not valid SPARQL. {violation.reason}")
    for problem in schema_problems:
        lines.append(f"Your previous query uses something the data does not contain. {problem.message}")
    compliance = [violation for violation in gate_b_violations if violation.rule != Rule.INVALID_SPARQL]
    if compliance and not lines:
        lines.append("Check that your previous query translates the request exactly. If the request itself asks "
                     "for something the rules forbid, keep it: translate literally and do not soften it.")
    if lines:
        lines.append("Write the corrected query for the same request.")
    return "\n".join(lines)
