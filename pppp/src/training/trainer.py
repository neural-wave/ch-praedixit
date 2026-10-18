"""A training loop for torch modules, organised like the Lightning Trainer.

The Trainer runs the loops and moves batches to the device; what happens in each step is
decided by the module. A module given to the Trainer is a torch.nn.Module that defines:
- forward(batch) -> logits;
- training_step(batch) -> loss tensor;
- validation_step(batch) -> {metric name: value averaged over the batch}, with a "loss" entry;
- predict_step(batch) -> predictions tensor (one row per item of the batch);
- configure_optimizers(total_steps) -> (optimizer, learning rate scheduler).

A data module defines train_dataloader() and val_dataloader(). Batches are dicts of tensors;
labelled batches carry the targets under "labels".
"""

import time

import torch
from tqdm import tqdm


def choose_device():
    """Return the Apple GPU (MPS) or CUDA device when available, otherwise the CPU."""
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


class Trainer:
    """Fits a module on a data module, validating at the end of every epoch, and evaluates it.

    After fit(), the module holds the weights of the epoch with the lowest validation loss;
    `best_epoch` and `history` ([{"epoch", "train_loss", <validation metrics>}, ...]) describe the run.
    """

    def __init__(self, epochs=1, gradient_clip=1.0, device=None):
        self.epochs = epochs
        self.gradient_clip = gradient_clip
        if device is None:
            self.device = choose_device()
        else:
            self.device = device
        self.best_epoch = None
        self.history = []

    def move_to_device(self, batch):
        """Return the batch with every tensor on the trainer device."""
        batch_on_device = {}
        for name, tensor in batch.items():
            batch_on_device[name] = tensor.to(self.device)
        return batch_on_device

    def fit(self, module, datamodule):
        """Train `module` for `epochs` epochs on datamodule.train_dataloader().

        At the end of every epoch the module is validated on datamodule.val_dataloader(); the
        weights of the epoch with the lowest validation loss are restored at the end.
        """
        module.to(self.device)
        training_loader = datamodule.train_dataloader()
        validation_loader = datamodule.val_dataloader()
        total_steps = self.epochs * len(training_loader)
        optimizer, scheduler = module.configure_optimizers(total_steps)

        best_validation_loss = float("inf")
        best_weights = None
        self.history = []
        for epoch_number in range(1, self.epochs + 1):
            started_at = time.perf_counter()
            train_loss = self.train_one_epoch(module, training_loader, optimizer, scheduler, epoch_number)
            validation_metrics = self.validate(module, validation_loader)
            elapsed_seconds = time.perf_counter() - started_at

            epoch_record = {"epoch": epoch_number, "train_loss": train_loss}
            epoch_record.update(validation_metrics)
            self.history.append(epoch_record)
            metric_texts = []
            for name, value in validation_metrics.items():
                metric_texts.append(f"validation {name} {value:.4f}")
            print(f"epoch {epoch_number}/{self.epochs}: train loss {train_loss:.4f}, "
                  f"{', '.join(metric_texts)}, {elapsed_seconds:.0f} s")

            if validation_metrics["loss"] < best_validation_loss:
                best_validation_loss = validation_metrics["loss"]
                self.best_epoch = epoch_number
                # A copy on the CPU, so that later epochs do not overwrite it.
                best_weights = {}
                for name, tensor in module.state_dict().items():
                    best_weights[name] = tensor.detach().cpu().clone()

        module.load_state_dict(best_weights)
        print(f"kept the weights of epoch {self.best_epoch} (validation loss {best_validation_loss:.4f})")

    def train_one_epoch(self, module, training_loader, optimizer, scheduler, epoch_number):
        """Run one pass of training_step over `training_loader`; return the mean training loss."""
        module.train()
        loss_sum = 0.0
        batch_count = 0
        progress_bar = tqdm(training_loader, desc=f"epoch {epoch_number}/{self.epochs}", unit="batch")
        for batch in progress_bar:
            loss = module.training_step(self.move_to_device(batch))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(module.parameters(), self.gradient_clip)
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad()
            loss_sum += loss.item()
            batch_count += 1
            progress_bar.set_postfix(mean_loss=f"{loss_sum / batch_count:.4f}")
        return loss_sum / batch_count

    def validate(self, module, dataloader):
        """Return the validation_step metrics of `module` on `dataloader`, averaged over all items."""
        module.to(self.device)
        module.eval()
        weighted_sums = {}
        item_count = 0
        with torch.no_grad():
            for batch in tqdm(dataloader, desc="validating", unit="batch", leave=False):
                batch_metrics = module.validation_step(self.move_to_device(batch))
                batch_size = len(batch["labels"])
                for name, value in batch_metrics.items():
                    weighted_sums[name] = weighted_sums.get(name, 0.0) + value * batch_size
                item_count += batch_size

        averaged_metrics = {}
        for name, weighted_sum in weighted_sums.items():
            averaged_metrics[name] = weighted_sum / item_count
        return averaged_metrics

    def predict(self, module, dataloader):
        """Return the predict_step outputs of `module` on `dataloader`, concatenated on the CPU."""
        module.to(self.device)
        module.eval()
        prediction_batches = []
        with torch.no_grad():
            for batch in tqdm(dataloader, desc="predicting", unit="batch", leave=False):
                prediction_batches.append(module.predict_step(self.move_to_device(batch)).cpu())
        return torch.cat(prediction_batches)

    def predict_logits(self, module, dataloader):
        """Return (logits, labels) of `module` on a labelled `dataloader`, concatenated on the CPU."""
        module.to(self.device)
        module.eval()
        logits_batches = []
        labels_batches = []
        with torch.no_grad():
            for batch in tqdm(dataloader, desc="predicting", unit="batch", leave=False):
                logits_batches.append(module(self.move_to_device(batch)).float().cpu())
                labels_batches.append(batch["labels"])
        return torch.cat(logits_batches), torch.cat(labels_batches)

    def evaluate(self, module, dataloader, metrics):
        """Return metrics.compute() after adding the predictions of `module` on `dataloader`.

        `metrics` defines reset(), update(predictions, examples) and compute(); `examples` is
        dataloader.dataset.examples, in the order of the predictions (the loader must not shuffle).
        """
        predictions = self.predict(module, dataloader)
        metrics.reset()
        metrics.update(predictions, dataloader.dataset.examples)
        return metrics.compute()
