"""Fine-tune the Gate A intent classifier on the "train" set and save it.

The classifier is fitted with pppp.training.trainer.Trainer, validated on "dev" at the end of
every epoch (the weights of the best epoch are kept), then calibrated: its temperature is
fitted on "dev". The model is saved in --output-directory, by default
models/gate_a/<base model name>/.

Usage:
    python scripts/train/train_gate_a.py
    python scripts/train/train_gate_a.py --base-model answerdotai/ModernBERT-large --learning-rate 2e-5
"""

import argparse
import pathlib

import torch

from pppp.checker.GateA.data import IntentDataModule
from pppp.checker.GateA.intent_rules import INTENT_RULES
from pppp.checker.GateA.model import IntentClassifier
from pppp.training.trainer import Trainer

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
DEFAULT_OUTPUT_ROOT = REPOSITORY_ROOT / "models" / "gate_a"


def parse_arguments():
    """Return the command line arguments of the training run."""
    parser = argparse.ArgumentParser(description="Fine-tune the Gate A intent classifier.")
    parser.add_argument("--base-model", default="answerdotai/ModernBERT-base",
                        help="Hugging Face id or local path of the pretrained encoder")
    parser.add_argument("--output-directory", type=pathlib.Path, default=None,
                        help="where to save the model (default: models/gate_a/<base model name>/)")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--learning-rate", type=float, default=3e-5)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--max-tokens", type=int, default=128,
                        help="requests longer than this are truncated")
    parser.add_argument("--seed", type=int, default=0)
    return parser.parse_args()


def main():
    arguments = parse_arguments()
    if arguments.output_directory is None:
        # "answerdotai/ModernBERT-base" is saved in models/gate_a/ModernBERT-base/.
        base_model_name = arguments.base_model.rstrip("/").split("/")[-1]
        output_directory = DEFAULT_OUTPUT_ROOT / base_model_name
    else:
        output_directory = arguments.output_directory
    torch.manual_seed(arguments.seed)

    classifier = IntentClassifier.from_pretrained_encoder(
        arguments.base_model, INTENT_RULES,
        max_tokens=arguments.max_tokens, learning_rate=arguments.learning_rate,
    )
    datamodule = IntentDataModule(classifier.tokenize, batch_size=arguments.batch_size, seed=arguments.seed)
    datamodule.setup()
    trainer = Trainer(epochs=arguments.epochs)

    trainer.fit(classifier, datamodule)
    dev_logits, dev_labels = trainer.predict_logits(classifier, datamodule.val_dataloader())
    classifier.fit_temperature(dev_logits, dev_labels)
    print(f"temperature fitted on dev: {classifier.temperature:.2f}")

    training_settings = {
        "epochs": arguments.epochs,
        "best_epoch": trainer.best_epoch,
        "batch_size": arguments.batch_size,
        "seed": arguments.seed,
        "train_examples": len(datamodule.sets_by_name["train"].examples),
        "history": trainer.history,
    }
    classifier.to("cpu").save(output_directory, training_settings)
    print(f"saved to {output_directory}")


if __name__ == "__main__":
    main()
