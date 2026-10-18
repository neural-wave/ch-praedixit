"""Export generated entries of data/generated.jsonl as one `.rq` file per entry.

Each line of data/generated.jsonl is one generated entry: a natural language request
(`request`), its SPARQL query (`sparql`), the expected verdict (`expected_compliant`,
`expected_rule`), the split it belongs to (`split`) and the template it was generated from
(`family`, `concept`, `variant`).

The entries of each requested split are written to data/generated_rq/<split>/ in the format of
data/evaluation_set/ (see its README.md): a `# field: value` header followed by the query, so
they can be run with query.sh or read by pppp.checker.gate_b_checker.

Usage: python scripts/dataset_generator/export_compliance_jsonl.py [split ...]   (default: train dev test)
"""

import json
import pathlib
import sys

# This file is at <repository>/scripts/dataset_generator/export_compliance_jsonl.py.
REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
GENERATED_PATH = REPOSITORY_ROOT / "data" / "generated.jsonl"
OUTPUT_DIRECTORY = REPOSITORY_ROOT / "data" / "generated_rq"
SPLIT_NAMES = ["train", "dev", "test"]


def file_slug(entry):
    """Return the file name slug 'family-concept-variant' of an entry, with '-' instead of '_'."""
    slug_parts = [entry["family"], entry["concept"], entry["variant"]]
    return "-".join(slug_parts).replace("_", "-")


def entry_file_name(entry):
    """Return the `.rq` file name of an entry: verdict prefix, entry id, slug."""
    if entry["expected_compliant"]:
        verdict_prefix = "compliant"
    else:
        verdict_prefix = "non-compliant"
    return f"{verdict_prefix}-{entry['id']}-{file_slug(entry)}.rq"


def entry_file_text(entry):
    """Return the `.rq` file content of an entry: the header lines followed by the query."""
    if entry["expected_compliant"]:
        expected_compliant = "true"
    else:
        expected_compliant = "false"

    header_lines = [
        f"# question: {entry['request']}",
        f"# expected_compliant: {expected_compliant}",
        f"# expected_rules: {entry['expected_rule']}",
        f"# origin: generated.jsonl {entry['id']}",
        f"# notes: family={entry['family']}, concept={entry['concept']}, "
        f"variant={entry['variant']}, request_kind={entry['request_kind']}",
    ]
    return "\n".join(header_lines) + "\n" + entry["sparql"].strip() + "\n"


def export_split(entries, split_name):
    """Write one `.rq` file per entry of `split_name` into data/generated_rq/<split>/; return how many."""
    output_directory = OUTPUT_DIRECTORY / split_name
    output_directory.mkdir(parents=True, exist_ok=True)

    written_count = 0
    for entry in entries:
        if entry["split"] != split_name:
            continue
        output_path = output_directory / entry_file_name(entry)
        output_path.write_text(entry_file_text(entry), encoding="utf-8")
        written_count += 1
    return written_count


def main():
    if len(sys.argv) > 1:
        requested_splits = sys.argv[1:]
    else:
        requested_splits = SPLIT_NAMES

    entries = []
    for jsonl_line in GENERATED_PATH.read_text(encoding="utf-8").splitlines():
        if jsonl_line.strip():
            entries.append(json.loads(jsonl_line))

    for split_name in requested_splits:
        written_count = export_split(entries, split_name)
        print(f"{split_name}: {written_count} files in {OUTPUT_DIRECTORY / split_name}")


if __name__ == "__main__":
    main()
