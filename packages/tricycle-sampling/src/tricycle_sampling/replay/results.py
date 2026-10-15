"""Explicit replay result delivery; training inputs and caches stay on the worker."""
from __future__ import annotations

import shutil
from pathlib import Path

# Generated configuration and training diagnostics are results. Refit input
# weights, raw/preprocessed training tables and duplicate full-model weights are not.
_TRAINING_FILES = (
    "config.yaml", "split.json", "train.log", "training_summary.json",
    "model/history.json", "model/resolved_run.json", "model/split.json",
    "model/training_protocol.json", "model/training_summary.json",
    "model/preprocessing_report.json", "model/preprocessing_anomalies.csv",
)


def publish_training_results(work: Path, output: Path) -> None:
    for role in ("full-fit", "validation-fit"):
        names = (*_TRAINING_FILES, *(("model/checkpoint.pt", "checkpoint.pt")
                                     if role == "validation-fit" else ()))
        for name in names:
            source = work / role / name
            if source.is_file():
                destination = output / role / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
