"""Compliance rules and the violations the structural checks report."""

import dataclasses
import enum


class Rule(str, enum.Enum):
    """A compliance rule (see RULES.md).

    The rules of Query_Compliance.md have the values of `expected_rules` in the compliance test
    set. Each new rule of RULES.md refines one of them, named in its comment: an entry that
    breaks it is labelled with that rule in the test set.
    """

    NONE = "none"
    NOT_READ_ONLY = "not_read_only"
    NOT_SELECT = "not_select"
    NOT_AGGREGATED = "not_aggregated"
    EXPOSES_IDENTIFIER = "exposes_identifier"
    SINGLE_PATIENT = "single_patient"
    UNRESTRICTED_MATCH = "unrestricted_match"
    INVALID_SPARQL = "invalid_sparql"
    # New rules (RULES.md).
    # A HAVING keeps only groups of 5 patients or fewer: single_patient.
    SMALL_GROUP = "small_group"
    # Results grouped by an exact value or date-time: each group is one record, not_aggregated.
    GROUPED_BY_EXACT_VALUE = "grouped_by_exact_value"
    # A specific patient is excluded: compared with the full count it reveals them, single_patient.
    EXCLUDES_PATIENT = "excludes_patient"
    # An exact timestamp or a full date is fixed: single_patient.
    EXACT_DATE = "exact_date"
    # 3 or more identifying attributes are fixed to exact values: single_patient.
    QUASI_IDENTIFIERS = "quasi_identifiers"
    # One result row per patient, even without identifiers: not_aggregated.
    ROW_PER_PATIENT = "row_per_patient"


class RuleSet(str, enum.Enum):
    """Which rules Gate B enforces (the `rules` argument of check_query).

    base: the rules of Query_Compliance.md (SELECT only, aggregated results, no identifiers, not
        about a single patient, no unrestricted match), plus valid and read-only SPARQL;
    extended: base plus the six new rules of RULES.md. The default.
    """

    BASE = "base"
    EXTENDED = "extended"


# The new rules of RULES.md.
NEW_RULES = frozenset({
    Rule.SMALL_GROUP, Rule.GROUPED_BY_EXACT_VALUE, Rule.EXCLUDES_PATIENT,
    Rule.EXACT_DATE, Rule.QUASI_IDENTIFIERS, Rule.ROW_PER_PATIENT,
})
# New rules that only rename what a base check already found: grouping by a patient or by a raw
# record value (not_aggregated), and comparing a patient graph or identifier with a constant
# through != (single_patient). With the base rules they are reported under the original rule.
# The other new rules come from checks of their own, which only run with the extended rules.
BASE_RULE_OF = {
    Rule.GROUPED_BY_EXACT_VALUE: Rule.NOT_AGGREGATED,
    Rule.ROW_PER_PATIENT: Rule.NOT_AGGREGATED,
    Rule.EXCLUDES_PATIENT: Rule.SINGLE_PATIENT,
}


@dataclasses.dataclass(frozen=True)
class Violation:
    """A rule the query certainly breaks, with a reason a person can read."""

    rule: Rule
    reason: str
