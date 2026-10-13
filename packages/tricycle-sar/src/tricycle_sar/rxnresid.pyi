"""Static interface for the accompanying runtime implementation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

def load_route_vocabulary(path: Path) -> dict[str, int]: ...
def freeze_route_vocabulary(prior: Path, output: Path) -> None: ...
def run_native(source: Path, entrypoint: str, vocabulary_path: Path, argv: list[str]) -> None: ...
def load_base_config(source_directory: Path) -> dict[str, Any]: ...
def write_round_config(
    path: Path,
    base_config: Mapping[str, Any],
    *,
    training_csv: Path,
    split_manifest: Path,
    output_dir: Path,
    epochs: int,
    batch_size_paths: int,
    device: str,
    mixed_precision: str,
    seed: int,
) -> None: ...
def write_all_train_manifest(
    training_csv: Path, manifest_path: Path, *, route_vocabulary: Path | None = ...
) -> int: ...
def prepare_refit_checkpoint(
    previous_checkpoint: Path,
    refit_checkpoint: Path,
    *,
    training_csv: Path,
    split_manifest: Path,
    fixed_seed: int,
) -> None: ...
def route_command(
    command: Sequence[str],
    *,
    source_directory: Path | None = ...,
    route_vocabulary: Path | None = ...,
) -> tuple[str, ...]: ...
def main() -> None: ...
def fit_all_revealed(
    data: Path,
    out: Path,
    recipe: Mapping[str, Any],
    *,
    source: Path,
    previous: Path | None = ...,
    route_vocabulary: Path | None = ...,
) -> Path: ...
