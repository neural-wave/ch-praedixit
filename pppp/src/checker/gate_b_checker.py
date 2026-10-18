"""Run the structural compliance checks (Gate B) on the compliance test set and print the errors.

For each entry of data/evaluation_set/ the violations reported by B1 are compared with
the expected verdict in the entry header (see data/evaluation_set/README.md):
- a compliant entry with violations is wrongly rejected, which B1 must never do;
- a non-compliant entry without violations is not caught by B1 (a later gate may catch it);
- ambiguous entries are listed with what B1 reports.

Usage: python -m pppp.checker.gate_b_checker [test set directory] [--rules extended|base]
"""

import argparse
import pathlib
import re

from pppp.checker.GateB import check_query

# This file is at <repository>/pppp/src/checker/gate_b_checker.py.
REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_TEST_SET_DIRECTORY = REPOSITORY_ROOT / "data" / "evaluation_set"
HEADER_LINE = re.compile(r"^# (\w+): (.*)$")


def read_header(entry_text):
    """Return {field: value} from the leading `# field: value` lines of a test set entry."""
    header = {}
    for line in entry_text.splitlines():
        header_match = HEADER_LINE.match(line)
        if header_match is None:
            break
        header[header_match.group(1)] = header_match.group(2).strip()
    return header


def reported_rules_text(violations):
    """Return the distinct rules of a list of Violation, comma-separated, in reporting order."""
    rule_names = []
    for violation in violations:
        if violation.rule.value not in rule_names:
            rule_names.append(violation.rule.value)
    return ", ".join(rule_names)


def print_entry(file_name, expected_rules, violations, show_reasons):
    """Print one entry as 'file: expected -> reported', optionally followed by the reasons."""
    print(f"  {file_name}")
    print(f"      expected: {expected_rules}   B1: {reported_rules_text(violations) or '-'}")
    if not show_reasons:
        return
    for violation in violations:
        print(f"      - {violation.reason}")


def main():
    parser = argparse.ArgumentParser(description="Run Gate B on a labelled test set and print its errors.")
    parser.add_argument("test_set_directory", nargs="?", type=pathlib.Path, default=DEFAULT_TEST_SET_DIRECTORY)
    parser.add_argument("--rules", choices=["extended", "base"], default="extended",
                        help="extended (default): every rule; base: only the rules of Query_Compliance.md "
                             "(entries labelled by the new rules then show up as not caught)")
    arguments = parser.parse_args()
    test_set_directory = arguments.test_set_directory

    # Each list holds (file name, expected rules, violations reported by B1).
    wrongly_rejected = []
    accepted_compliant = []
    caught_non_compliant = []
    missed_non_compliant = []
    ambiguous_entries = []

    entry_paths = sorted(test_set_directory.glob("*.rq"))
    for entry_path in entry_paths:
        entry_text = entry_path.read_text(encoding="utf-8")
        header = read_header(entry_text)
        expected_verdict = header["expected_compliant"]
        expected_rules = header["expected_rules"]
        violations = check_query(entry_text, rules=arguments.rules)
        result = (entry_path.name, expected_rules, violations)

        if expected_verdict == "true":
            if violations:
                wrongly_rejected.append(result)
            else:
                accepted_compliant.append(result)
        elif expected_verdict == "false":
            if violations:
                caught_non_compliant.append(result)
            else:
                missed_non_compliant.append(result)
        else:
            ambiguous_entries.append(result)

    compliant_count = len(wrongly_rejected) + len(accepted_compliant)
    non_compliant_count = len(caught_non_compliant) + len(missed_non_compliant)
    ambiguous_rejected_count = 0
    for file_name, expected_rules, violations in ambiguous_entries:
        if violations:
            ambiguous_rejected_count += 1

    print(f"B1 on {len(entry_paths)} entries of {test_set_directory}, {arguments.rules} rules\n")
    print(f"compliant:     {compliant_count:3}   wrongly rejected: {len(wrongly_rejected)}")
    print(f"non-compliant: {non_compliant_count:3}   caught: {len(caught_non_compliant)}   "
          f"not caught: {len(missed_non_compliant)}")
    print(f"ambiguous:     {len(ambiguous_entries):3}   rejected: {ambiguous_rejected_count}   "
          f"passed: {len(ambiguous_entries) - ambiguous_rejected_count}")

    print("\nWrongly rejected compliant entries:")
    if not wrongly_rejected:
        print("  (none)")
    for file_name, expected_rules, violations in wrongly_rejected:
        print_entry(file_name, expected_rules, violations, show_reasons=True)
    #
    # # print("\nCaught non-compliant entries:")
    # for file_name, expected_rules, violations in caught_non_compliant:
    #     print_entry(file_name, expected_rules, violations, show_reasons=True)
    #
    print("\nNon-compliant entries not caught:")
    for file_name, expected_rules, violations in missed_non_compliant:
        print_entry(file_name, expected_rules, violations, show_reasons=False)
    #
    # # print("\nAmbiguous entries:")
    # for file_name, expected_rules, violations in ambiguous_entries:
    #     print_entry(file_name, expected_rules, violations, show_reasons=True)


if __name__ == "__main__":
    main()
