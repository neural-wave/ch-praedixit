"""The Gate A intent classifier: a pretrained encoder with one output per intent rule.

IntentClassifier plays the role of a Lightning module: besides the network it defines the
steps the Trainer calls (training_step, validation_step, predict_step), the optimizer
(configure_optimizers), and how it is saved and loaded.

Calibration: the logits are divided by a temperature (at least 1) before the softmax. A
network trained with cross-entropy tends to be overconfident; the temperature fitted on
held-out data makes P(non-compliant) = 1 - P("none") closer to how often the request really is
non-compliant. It changes how confident the probabilities are, never which intent rule is the
most probable.

A saved model is a directory with the encoder weights, its configuration and tokenizer
(Hugging Face save_pretrained) and gate_a_settings.json:
{"base_model", "intent_rules", "temperature", "max_tokens", "learning_rate", ...training settings}.
"""

import json
import math
import pathlib

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer, get_linear_schedule_with_warmup

SETTINGS_FILE_NAME = "gate_a_settings.json"


class IntentClassifier(torch.nn.Module):
    """Classifies a request text into one of `intent_rules`, with calibrated probabilities."""

    def __init__(self, encoder, tokenizer, base_model, intent_rules, max_tokens, learning_rate, temperature=1.0):
        super().__init__()
        self.encoder = encoder
        self.tokenizer = tokenizer
        self.base_model = base_model
        self.intent_rules = intent_rules
        self.max_tokens = max_tokens
        self.learning_rate = learning_rate
        self.temperature = temperature

    @classmethod
    def from_pretrained_encoder(cls, base_model, intent_rules, max_tokens=128, learning_rate=3e-5):
        """Return a classifier made of the pretrained encoder `base_model` and a new, untrained head.

        `base_model` is a Hugging Face id or a local path of any encoder that supports sequence
        classification, e.g. "answerdotai/ModernBERT-base" or "answerdotai/ModernBERT-large".
        """
        labels_by_index = {}
        indices_by_label = {}
        for label_index, intent_rule in enumerate(intent_rules):
            labels_by_index[label_index] = intent_rule
            indices_by_label[intent_rule] = label_index
        tokenizer = AutoTokenizer.from_pretrained(base_model)
        # The intent rule names are stored in the encoder configuration, so the order of the
        # outputs travels with the saved weights.
        encoder = AutoModelForSequenceClassification.from_pretrained(
            base_model,
            num_labels=len(intent_rules),
            id2label=labels_by_index,
            label2id=indices_by_label,
        )
        return cls(encoder, tokenizer, base_model, intent_rules, max_tokens, learning_rate)

    @classmethod
    def load(cls, directory):
        """Return the classifier saved in `directory` by save()."""
        directory = pathlib.Path(directory)
        settings_path = directory / SETTINGS_FILE_NAME
        gate_a_settings = json.loads(settings_path.read_text(encoding="utf-8"))
        tokenizer = AutoTokenizer.from_pretrained(directory)
        encoder = AutoModelForSequenceClassification.from_pretrained(directory)
        return cls(
            encoder,
            tokenizer,
            base_model=gate_a_settings["base_model"],
            intent_rules=gate_a_settings["intent_rules"],
            max_tokens=gate_a_settings["max_tokens"],
            learning_rate=gate_a_settings["learning_rate"],
            temperature=gate_a_settings["temperature"],
        )

    def save(self, directory, training_settings):
        """Write the weights, the tokenizer and gate_a_settings.json into `directory`.

        `training_settings` is a dict of extra values to record (epochs, seed, ...).
        """
        directory = pathlib.Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.encoder.save_pretrained(directory)
        self.tokenizer.save_pretrained(directory)
        gate_a_settings = {
            "base_model": self.base_model,
            "intent_rules": self.intent_rules,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "learning_rate": self.learning_rate,
        }
        gate_a_settings.update(training_settings)
        settings_path = directory / SETTINGS_FILE_NAME
        settings_path.write_text(json.dumps(gate_a_settings, indent=2) + "\n", encoding="utf-8")

    def tokenize(self, request_texts):
        """Return the model inputs of a list of request texts: {"input_ids", "attention_mask"} tensors."""
        encoded_texts = self.tokenizer(request_texts, truncation=True, max_length=self.max_tokens,
                                       padding=True, return_tensors="pt")
        return {"input_ids": encoded_texts["input_ids"], "attention_mask": encoded_texts["attention_mask"]}

    def forward(self, batch):
        """Return the uncalibrated logits (requests x intent rules) of a batch of model inputs."""
        return self.encoder(input_ids=batch["input_ids"], attention_mask=batch["attention_mask"]).logits

    def probabilities(self, logits):
        """Return the calibrated probability of each intent rule: softmax(logits / temperature)."""
        return torch.softmax(logits.float() / self.temperature, dim=1)

    def training_step(self, batch):
        """Return the cross-entropy loss of a labelled batch."""
        logits = self(batch)
        return torch.nn.functional.cross_entropy(logits, batch["labels"])

    def validation_step(self, batch):
        """Return {"loss", "accuracy"} of a labelled batch, both averaged over its requests."""
        logits = self(batch)
        loss = torch.nn.functional.cross_entropy(logits, batch["labels"])
        accuracy = (logits.argmax(dim=1) == batch["labels"]).float().mean()
        return {"loss": loss.item(), "accuracy": accuracy.item()}

    def predict_step(self, batch):
        """Return the calibrated probabilities (requests x intent rules) of a batch."""
        return self.probabilities(self(batch))

    def configure_optimizers(self, total_steps):
        """Return (optimizer, scheduler): AdamW, with a learning rate that rises over the first
        10% of the steps and then falls linearly to zero."""
        optimizer = torch.optim.AdamW(self.parameters(), lr=self.learning_rate, weight_decay=0.01)
        scheduler = get_linear_schedule_with_warmup(optimizer, int(0.1 * total_steps), total_steps)
        return optimizer, scheduler

    def fit_temperature(self, logits, labels):
        """Set the temperature in [1, 10] that gives the lowest log-loss on (logits, labels).

        `logits` are uncalibrated (requests x intent rules), `labels` the intent rule indices.
        """
        best_temperature = 1.0
        best_log_loss = math.inf
        # Temperatures below 1 would make the model more confident than training left it. When
        # the held-out set has no errors (template wording is easy) the log-loss always favours a
        # sharper model, so the search would end at the lower bound and every probability would
        # become 0 or 1. 100 temperatures evenly spaced on a logarithmic scale.
        candidate_temperatures = torch.exp(torch.linspace(0.0, math.log(10.0), 100))
        for temperature in candidate_temperatures.tolist():
            log_loss = torch.nn.functional.cross_entropy(logits.float() / temperature, labels).item()
            if log_loss < best_log_loss:
                best_log_loss = log_loss
                best_temperature = temperature
        self.temperature = best_temperature
