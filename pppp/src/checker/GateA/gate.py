"""Gate A at serving time: answers one request with a typed, Jev-style answer and a decision.

The answer has two typed fields, as in Jev's API:
- "compliant": a noul, P(the request is acceptable) = P("none");
- "rule": a choice among the intent rules, with the probability of each.
Gate A refuses the request early when P(non-compliant) = 1 - P("none") reaches the threshold;
otherwise the request goes on to SPARQL generation.
"""

import dataclasses

import torch

from pppp.checker.GateA.model import IntentClassifier

REFUSAL_THRESHOLD = 0.9


@dataclasses.dataclass(frozen=True)
class GateADecision:
    """The decision on one request; `answer` is the typed answer (see jev_style_answer).

    tokens_used: the number of tokens the classifier read for the request.
    """

    refused: bool
    non_compliant_probability: float
    rule: str
    answer: dict
    tokens_used: int


def jev_style_answer(probability_row, intent_rules):
    """Return the typed answer of one request from its probability per intent rule.

    Shape: {"compliant": {"type": "noul", "noul": P(none)},
            "rule": {"type": "choice", "choice": most probable rule, "probabilities": {rule: p}}}
    """
    probabilities_by_rule = {}
    for rule_index, intent_rule in enumerate(intent_rules):
        probabilities_by_rule[intent_rule] = round(probability_row[rule_index].item(), 4)
    most_probable_rule = intent_rules[probability_row.argmax().item()]
    return {
        "compliant": {"type": "noul", "noul": probabilities_by_rule["none"]},
        "rule": {"type": "choice", "choice": most_probable_rule, "probabilities": probabilities_by_rule},
    }


class IntentGate:
    """Decides, from the request text alone, whether to refuse a request before generating SPARQL."""

    def __init__(self, classifier, refusal_threshold=REFUSAL_THRESHOLD):
        self.classifier = classifier
        self.refusal_threshold = refusal_threshold
        self.classifier.eval()

    @classmethod
    def load(cls, model_directory, refusal_threshold=REFUSAL_THRESHOLD):
        """Return a gate using the classifier saved in `model_directory`."""
        return cls(IntentClassifier.load(model_directory), refusal_threshold)

    def decide(self, request_text):
        """Return the GateADecision for one request text."""
        model_inputs = self.classifier.tokenize([request_text])
        with torch.no_grad():
            logits = self.classifier(model_inputs)
            probability_row = self.classifier.probabilities(logits)[0]
        # The attention mask is 1 on every token of the request, special tokens included.
        tokens_used = int(model_inputs["attention_mask"].sum().item())
        answer = jev_style_answer(probability_row, self.classifier.intent_rules)
        non_compliant_probability = 1 - answer["compliant"]["noul"]
        return GateADecision(
            refused=non_compliant_probability >= self.refusal_threshold,
            non_compliant_probability=non_compliant_probability,
            rule=answer["rule"]["choice"],
            answer=answer,
            tokens_used=tokens_used,
        )
