"""Measures of Gate A on a labelled set of requests.

Gate A refuses a request early, before any SPARQL is generated, when P(non-compliant) =
1 - P("none") reaches the refusal threshold. On a labelled set this gives:
- non-compliant refused: the non-compliant requests stopped early (the gain: the generator
  is skipped). A non-compliant request that passes is not an error, later gates judge it;
- false refusals: the compliant requests refused (the cost: nothing checks a Gate A refusal
  again, so each one is a wrong answer);
- right rule among refused: how often the most probable rule of a refused non-compliant
  request is its intent rule (it fills the reason of the refusal).
"""

import dataclasses


@dataclasses.dataclass(frozen=True)
class RequestOutcome:
    """What Gate A did with one request of the set."""

    example: object
    non_compliant_probability: float
    predicted_rule: str


@dataclasses.dataclass
class GateAOutcome:
    """The counts of one set; `false_refusals` and `missed` hold RequestOutcome items."""

    compliant_count: int = 0
    non_compliant_count: int = 0
    refused_non_compliant_count: int = 0
    right_rule_count: int = 0
    false_refusals: list = dataclasses.field(default_factory=list)
    missed: list = dataclasses.field(default_factory=list)


class GateAMetrics:
    """Accumulates Gate A decisions over batches of predictions, then returns a GateAOutcome."""

    def __init__(self, intent_rules, refusal_threshold):
        self.intent_rules = intent_rules
        self.refusal_threshold = refusal_threshold
        self.outcome = GateAOutcome()

    def reset(self):
        """Forget every prediction seen so far."""
        self.outcome = GateAOutcome()

    def update(self, probabilities, examples):
        """Add the predictions of a set: `probabilities` (requests x intent rules) row i is examples[i]."""
        none_index = self.intent_rules.index("none")
        for example_index, example in enumerate(examples):
            probability_row = probabilities[example_index]
            non_compliant_probability = 1 - probability_row[none_index].item()
            predicted_rule = self.intent_rules[probability_row.argmax().item()]
            refused = non_compliant_probability >= self.refusal_threshold
            request_outcome = RequestOutcome(example, non_compliant_probability, predicted_rule)

            if example.intent_rule == "none":
                self.outcome.compliant_count += 1
                if refused:
                    self.outcome.false_refusals.append(request_outcome)
                continue

            self.outcome.non_compliant_count += 1
            if not refused:
                self.outcome.missed.append(request_outcome)
                continue
            self.outcome.refused_non_compliant_count += 1
            if predicted_rule == example.intent_rule:
                self.outcome.right_rule_count += 1

    def compute(self):
        """Return the GateAOutcome of every prediction added since the last reset."""
        return self.outcome
