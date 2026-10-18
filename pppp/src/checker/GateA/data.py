"""Request-level data for Gate A: examples, the torch Dataset and the data module.

An example pairs a request text with its intent rule (see intent_rules.py). The sets are:
- "train", "dev", "test": the splits of data/generated.jsonl;
- "hand_written", "adversarial", "external": data/evaluation_set/ grouped by the `origin`
  header (see data/evaluation_set/README.md), used for evaluation only.

Within each set a request text is kept once; a text that appears with two different intent
rules is dropped. A text of any other set that also appears in "train" is dropped from that
set, so that every set measures requests the model has not been trained on.

IntentDataModule plays the role of a Lightning data module: it loads the sets once and
returns the DataLoader of each of them.

Usage: python -m pppp.checker.GateA.data   (prints a summary of every set)
"""

import collections
import dataclasses
import json
import pathlib
import re

import torch
from torch.utils.data import DataLoader, Dataset

from pppp.checker.GateA.intent_rules import INTENT_RULES, evaluation_intent_rule, generated_intent_rule, rule_name

# This file is at <repository>/pppp/src/checker/GateA/data.py.
REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[4]
DEFAULT_DATA_DIRECTORY = REPOSITORY_ROOT / "data"
HEADER_LINE = re.compile(r"^# (\w+): (.*)$")

GENERATED_SPLITS = ["train", "dev", "test"]
EVALUATION_GROUPS = ["hand_written", "adversarial", "external"]


@dataclasses.dataclass(frozen=True)
class IntentExample:
    """A request text with its intent rule; `source` is the generated entry id or the .rq file name."""

    request: str
    intent_rule: str
    source: str


@dataclasses.dataclass
class IntentSet:
    """The examples of one set, and how many entries were skipped for each reason."""

    examples: list
    skipped_by_reason: collections.Counter


# ---------------------------------------------------------------------------------------------
# Loading the sets from data/
# ---------------------------------------------------------------------------------------------


def evaluation_group(origin):
    """Return the evaluation group of an `origin` header value."""
    if origin == "adversarial":
        return "adversarial"
    if origin.startswith("external LLM"):
        return "external"
    # "test set", "bypass" and "queries-example.md section N" are all written by hand.
    return "hand_written"


def read_header(entry_text):
    """Return {field: value} from the leading `# field: value` lines of an evaluation set entry."""
    header = {}
    for line in entry_text.splitlines():
        header_match = HEADER_LINE.match(line)
        if header_match is None:
            break
        header[header_match.group(1)] = header_match.group(2).strip()
    return header


def distinct_examples(candidate_examples, skipped_by_reason):
    """Return the candidates with one example per request text, in their original order.

    The first example of each text is kept. Texts that appear with two different intent rules
    are dropped and counted in `skipped_by_reason["conflicting labels"]`; repeated texts are
    counted in `skipped_by_reason["duplicate text"]`.
    """
    intent_rules_by_text = collections.defaultdict(set)
    for example in candidate_examples:
        intent_rules_by_text[example.request].add(example.intent_rule)

    examples = []
    kept_texts = set()
    for example in candidate_examples:
        if len(intent_rules_by_text[example.request]) > 1:
            skipped_by_reason["conflicting labels"] += 1
            continue
        if example.request in kept_texts:
            skipped_by_reason["duplicate text"] += 1
            continue
        kept_texts.add(example.request)
        examples.append(example)
    return examples


def load_generated_sets(generated_path):
    """Return {split name: IntentSet} for the train, dev and test splits of generated.jsonl."""
    candidates_by_split = {}
    skipped_by_split = {}
    for split_name in GENERATED_SPLITS:
        candidates_by_split[split_name] = []
        skipped_by_split[split_name] = collections.Counter()

    for jsonl_line in generated_path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(jsonl_line)
        split_name = entry["split"]
        intent_rule = generated_intent_rule(entry)
        if intent_rule is None:
            skipped_by_split[split_name][entry["expected_rule"]] += 1
            continue
        example = IntentExample(request=entry["request"].strip(), intent_rule=intent_rule, source=entry["id"])
        candidates_by_split[split_name].append(example)

    sets_by_name = {}
    for split_name in GENERATED_SPLITS:
        skipped_by_reason = skipped_by_split[split_name]
        examples = distinct_examples(candidates_by_split[split_name], skipped_by_reason)
        sets_by_name[split_name] = IntentSet(examples=examples, skipped_by_reason=skipped_by_reason)
    return sets_by_name


def load_evaluation_sets(evaluation_set_directory):
    """Return {group name: IntentSet} for the entries of evaluation_set/, grouped by origin."""
    candidates_by_group = {}
    skipped_by_group = {}
    for group_name in EVALUATION_GROUPS:
        candidates_by_group[group_name] = []
        skipped_by_group[group_name] = collections.Counter()

    entry_paths = sorted(evaluation_set_directory.glob("*.rq"))
    for entry_path in entry_paths:
        header = read_header(entry_path.read_text(encoding="utf-8"))
        group_name = evaluation_group(header["origin"])
        intent_rule = evaluation_intent_rule(header)
        if intent_rule is None:
            if header["expected_compliant"] == "false":
                skip_reason = rule_name(header["expected_rules"].split(",")[0])
            else:
                skip_reason = header["expected_compliant"]
            skipped_by_group[group_name][skip_reason] += 1
            continue
        example = IntentExample(request=header["question"], intent_rule=intent_rule, source=entry_path.name)
        candidates_by_group[group_name].append(example)

    sets_by_name = {}
    for group_name in EVALUATION_GROUPS:
        skipped_by_reason = skipped_by_group[group_name]
        examples = distinct_examples(candidates_by_group[group_name], skipped_by_reason)
        sets_by_name[group_name] = IntentSet(examples=examples, skipped_by_reason=skipped_by_reason)
    return sets_by_name


def load_rq_directory(directory):
    """Return the IntentSet of the .rq files found in `directory` and its subdirectories.

    Each file needs the `# question`, `# expected_compliant` and `# expected_rules` header
    lines of data/evaluation_set/README.md; files without a question are skipped.
    """
    candidate_examples = []
    skipped_by_reason = collections.Counter()
    entry_paths = sorted(pathlib.Path(directory).rglob("*.rq"))
    for entry_path in entry_paths:
        header = read_header(entry_path.read_text(encoding="utf-8"))
        if "question" not in header:
            skipped_by_reason["no question"] += 1
            continue
        intent_rule = evaluation_intent_rule(header)
        if intent_rule is None:
            if header["expected_compliant"] == "false":
                skip_reason = rule_name(header["expected_rules"].split(",")[0])
            else:
                skip_reason = header["expected_compliant"]
            skipped_by_reason[skip_reason] += 1
            continue
        example = IntentExample(request=header["question"], intent_rule=intent_rule, source=entry_path.name)
        candidate_examples.append(example)

    examples = distinct_examples(candidate_examples, skipped_by_reason)
    return IntentSet(examples=examples, skipped_by_reason=skipped_by_reason)


def drop_texts_in_train_from(intent_set, train_texts):
    """Remove from `intent_set` the examples whose request text is in `train_texts`.

    The removed examples are counted in `skipped_by_reason["also in train"]`.
    """
    examples_not_in_train = []
    for example in intent_set.examples:
        if example.request in train_texts:
            intent_set.skipped_by_reason["also in train"] += 1
            continue
        examples_not_in_train.append(example)
    intent_set.examples = examples_not_in_train


def train_texts_of(sets_by_name):
    """Return the set of request texts of the "train" set."""
    train_texts = set()
    for example in sets_by_name["train"].examples:
        train_texts.add(example.request)
    return train_texts


def drop_texts_in_train(sets_by_name):
    """Remove from every set except "train" the examples whose request text is also in "train"."""
    train_texts = train_texts_of(sets_by_name)
    for set_name, intent_set in sets_by_name.items():
        if set_name == "train":
            continue
        drop_texts_in_train_from(intent_set, train_texts)


def load_intent_sets(data_directory=DEFAULT_DATA_DIRECTORY):
    """Return {set name: IntentSet} for the generated splits followed by the evaluation groups."""
    sets_by_name = load_generated_sets(data_directory / "generated.jsonl")
    sets_by_name.update(load_evaluation_sets(data_directory / "evaluation_set"))
    # Requests without a clinical concept to fill in (such as "Wipe one patient's graph.")
    # come out identical from every split.
    drop_texts_in_train(sets_by_name)
    return sets_by_name


# ---------------------------------------------------------------------------------------------
# Dataset and data module
# ---------------------------------------------------------------------------------------------


class IntentDataset(Dataset):
    """The examples of one set; item i is (request text, index of its intent rule in INTENT_RULES)."""

    def __init__(self, examples):
        self.examples = examples

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, index):
        example = self.examples[index]
        return example.request, INTENT_RULES.index(example.intent_rule)


class IntentDataModule:
    """Loads the Gate A sets and returns a DataLoader for each of them.

    `tokenize` turns a list of request texts into model inputs, a dict of tensors
    ({"input_ids", "attention_mask"}); the batches add the intent rule indices as "labels".
    """

    def __init__(self, tokenize, batch_size, seed=0, data_directory=DEFAULT_DATA_DIRECTORY):
        self.tokenize = tokenize
        self.batch_size = batch_size
        self.seed = seed
        self.data_directory = data_directory
        self.sets_by_name = None

    def setup(self):
        """Load every set from the data directory; call once before asking for a DataLoader."""
        self.sets_by_name = load_intent_sets(self.data_directory)

    def add_rq_directory(self, set_name, directory):
        """Add the .rq files of `directory` as the set `set_name`, without the requests already in "train".

        Call after setup(); the set is then available through dataloader(set_name).
        """
        intent_set = load_rq_directory(directory)
        drop_texts_in_train_from(intent_set, train_texts_of(self.sets_by_name))
        self.sets_by_name[set_name] = intent_set

    def collate_batch(self, items):
        """Return the model inputs of a list of (request text, label index) items, with "labels"."""
        request_texts = []
        label_indices = []
        for request_text, label_index in items:
            request_texts.append(request_text)
            label_indices.append(label_index)
        batch = self.tokenize(request_texts)
        batch["labels"] = torch.tensor(label_indices)
        return batch

    def dataloader(self, set_name, shuffle=False):
        """Return the DataLoader of one set; `shuffle` reorders it at every epoch, reproducibly."""
        dataset = IntentDataset(self.sets_by_name[set_name].examples)
        shuffle_generator = torch.Generator().manual_seed(self.seed)
        return DataLoader(dataset, batch_size=self.batch_size, shuffle=shuffle,
                          collate_fn=self.collate_batch, generator=shuffle_generator)

    def train_dataloader(self):
        """Return the shuffled DataLoader of "train"."""
        return self.dataloader("train", shuffle=True)

    def val_dataloader(self):
        """Return the DataLoader of "dev"."""
        return self.dataloader("dev")

    def evaluation_dataloaders(self):
        """Return {set name: DataLoader} for "test" and the three evaluation groups."""
        dataloaders_by_set = {"test": self.dataloader("test")}
        for group_name in EVALUATION_GROUPS:
            dataloaders_by_set[group_name] = self.dataloader(group_name)
        return dataloaders_by_set


def print_summary(sets_by_name):
    """Print, for each set, the examples per intent rule and the skipped entries per reason."""
    for set_name, intent_set in sets_by_name.items():
        rule_counts = collections.Counter()
        for example in intent_set.examples:
            rule_counts[example.intent_rule] += 1
        non_compliant_count = len(intent_set.examples) - rule_counts["none"]

        print(f"{set_name}: {len(intent_set.examples)} examples "
              f"({rule_counts['none']} compliant, {non_compliant_count} non-compliant)")
        for intent_rule in INTENT_RULES:
            print(f"    {intent_rule:20} {rule_counts[intent_rule]}")
        for skip_reason, skipped_count in sorted(intent_set.skipped_by_reason.items()):
            print(f"    skipped, {skip_reason}: {skipped_count}")


if __name__ == "__main__":
    print_summary(load_intent_sets())
