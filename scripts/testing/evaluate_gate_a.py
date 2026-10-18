"""Measure a trained Gate A intent classifier, or show its decision on one request.

Without --request, the classifier is evaluated on "test" and the three evaluation groups
(see pppp/checker/GateA/data.py; "dev" is left out, it chose the epoch and the temperature),
or only on the .rq files of the directories given with --rq-directory, and the measures of
pppp/checker/GateA/metrics.py are printed, followed by every false refusal. An .rq file is used
only when its header has a `# question` line (Gate A reads the request, not the query).

Usage:
    python scripts/testing/evaluate_gate_a.py models/gate_a/ModernBERT-base [--show-missed] [--threshold 0.95]
    python scripts/testing/evaluate_gate_a.py models/gate_a/ModernBERT-base --rq-directory data/test-queries/examples
    python scripts/testing/evaluate_gate_a.py models/gate_a/ModernBERT-base --request "Which patients have type 2 diabetes?"
"""

import argparse
import json
import pathlib

from pppp.checker.GateA.data import IntentDataModule
from pppp.checker.GateA.gate import REFUSAL_THRESHOLD, IntentGate
from pppp.checker.GateA.metrics import GateAMetrics
from pppp.checker.GateA.model import IntentClassifier
from pppp.training.trainer import Trainer


def parse_arguments():
    """Return the command line arguments."""
    parser = argparse.ArgumentParser(description="Measure a trained Gate A intent classifier.")
    parser.add_argument("model_directory", type=pathlib.Path,
                        help="a directory written by scripts/train/train_gate_a.py")
    parser.add_argument("--request", default=None,
                        help="show the decision on this request instead of measuring the sets")
    parser.add_argument("--threshold", type=float, default=REFUSAL_THRESHOLD, help="refusal threshold")
    parser.add_argument("--rq-directory", type=pathlib.Path, action="append", default=[],
                        help="measure the .rq files of this directory (and its subdirectories) "
                             "instead of the default sets; can be repeated")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--show-missed", action="store_true",
                        help="also list the non-compliant requests that are not refused")
    return parser.parse_args()


def show_decision(model_directory, request_text, refusal_threshold):
    """Print the typed answer and the Gate A decision for one request."""
    gate = IntentGate.load(model_directory, refusal_threshold)
    decision = gate.decide(request_text)
    print(json.dumps(decision.answer, indent=2))
    if decision.refused:
        print(f"decision: early refusal, P(non-compliant) {decision.non_compliant_probability:.2f} "
              f">= {refusal_threshold:.2f}, rule {decision.rule}")
    else:
        print(f"decision: passed on to SPARQL generation, P(non-compliant) "
              f"{decision.non_compliant_probability:.2f} < {refusal_threshold:.2f}")


def print_skipped_entries(set_name, intent_set):
    """Print how many requests of a set are measured and how many entries were skipped, by reason."""
    print(f"{set_name}: {len(intent_set.examples)} requests measured")
    for skip_reason, skipped_count in sorted(intent_set.skipped_by_reason.items()):
        print(f"    skipped, {skip_reason}: {skipped_count}")


def share_text(part_count, total_count):
    """Return 'part/total (percentage)', or '-' when the total is zero."""
    if total_count == 0:
        return "-"
    return f"{part_count}/{total_count} ({100 * part_count / total_count:.1f}%)"


def print_outcome_table(outcomes_by_set):
    """Print one row per set with the three Gate A measures."""
    print(f"\n{'set':14}{'non-compliant refused':>26}{'false refusals':>24}{'right rule among refused':>28}")
    for set_name, outcome in outcomes_by_set.items():
        refused_text = share_text(outcome.refused_non_compliant_count, outcome.non_compliant_count)
        false_refusals_text = share_text(len(outcome.false_refusals), outcome.compliant_count)
        right_rule_text = share_text(outcome.right_rule_count, outcome.refused_non_compliant_count)
        print(f"{set_name:14}{refused_text:>26}{false_refusals_text:>24}{right_rule_text:>28}")


def print_request_outcomes(title, request_outcomes_by_set):
    """Print the RequestOutcome items of every set: probability, predicted rule, source, request."""
    print(f"\n{title}")
    for set_name, request_outcomes in request_outcomes_by_set.items():
        if not request_outcomes:
            continue
        print(f"  {set_name}:")
        for request_outcome in request_outcomes:
            example = request_outcome.example
            print(f"    {request_outcome.non_compliant_probability:.2f}  "
                  f"{request_outcome.predicted_rule:18}  {example.source}")
            print(f"          expected {example.intent_rule}: {example.request}")


def main():
    arguments = parse_arguments()
    if arguments.request is not None:
        show_decision(arguments.model_directory, arguments.request, arguments.threshold)
        return

    classifier = IntentClassifier.load(arguments.model_directory)
    datamodule = IntentDataModule(classifier.tokenize, batch_size=arguments.batch_size)
    datamodule.setup()
    trainer = Trainer()
    metrics = GateAMetrics(classifier.intent_rules, arguments.threshold)

    if arguments.rq_directory:
        dataloaders_by_set = {}
        for rq_directory in arguments.rq_directory:
            set_name = rq_directory.name
            datamodule.add_rq_directory(set_name, rq_directory)
            print_skipped_entries(set_name, datamodule.sets_by_name[set_name])
            dataloaders_by_set[set_name] = datamodule.dataloader(set_name)
    else:
        dataloaders_by_set = datamodule.evaluation_dataloaders()

    outcomes_by_set = {}
    for set_name, dataloader in dataloaders_by_set.items():
        outcomes_by_set[set_name] = trainer.evaluate(classifier, dataloader, metrics)

    print(f"\nGate A {arguments.model_directory} (base {classifier.base_model}), "
          f"refusal threshold {arguments.threshold:.2f}, temperature {classifier.temperature:.2f}")
    print_outcome_table(outcomes_by_set)

    false_refusals_by_set = {}
    missed_by_set = {}
    for set_name, outcome in outcomes_by_set.items():
        false_refusals_by_set[set_name] = outcome.false_refusals
        missed_by_set[set_name] = outcome.missed
    print_request_outcomes("False refusals (P(non-compliant), predicted rule, source):", false_refusals_by_set)
    if arguments.show_missed:
        print_request_outcomes("Non-compliant requests not refused:", missed_by_set)


if __name__ == "__main__":
    main()
