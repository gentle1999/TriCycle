"""Static interface for the accompanying runtime implementation."""

from __future__ import annotations

from collections.abc import Sequence

def _non_empty(value: object, *, name: str) -> str: ...
def _extra_args(values: Sequence[str]) -> tuple[str, ...]: ...
def _optional_text(value: str | None, *, name: str) -> str | None: ...
def _positive(value: int | None, *, name: str) -> int | None: ...
def _positive_float(value: float | None, *, name: str) -> float | None: ...
def _append_option(
    command: tuple[str, ...], option: str, value: object | None
) -> tuple[str, ...]: ...
def build_training_command(
    *,
    python_executable: str = ...,
    script: str = ...,
    config: str = ...,
    output: str = ...,
    fold_index: int | None = ...,
    split_manifest: str | None = ...,
    epochs: int | None = ...,
    batch_size: int | None = ...,
    device: str | None = ...,
    mixed_precision: str | None = ...,
    seed: int | None = ...,
    model: str | None = ...,
    mapping_mode: str | None = ...,
    encoder: str | None = ...,
    path_encoder: str | None = ...,
    substrate_encoder: str | None = ...,
    product_encoder: str | None = ...,
    train_on_train_valid_from_scratch: bool = ...,
    two_stage_train_valid_from_scratch: bool = ...,
    selection_refit_from_scratch: bool = ...,
    stage1_epochs: int | None = ...,
    stage2_epochs: int | None = ...,
    stage2_learning_rate_factor: float | None = ...,
    stage2_auxiliary_weight_factor: float | None = ...,
    extra_args: Sequence[str] = ...,
) -> tuple[str, ...]: ...
def build_finetune_command(
    *,
    checkpoint: str,
    python_executable: str = ...,
    script: str = ...,
    output: str = ...,
    epochs: int | None = ...,
    batch_size: int | None = ...,
    device: str | None = ...,
    mixed_precision: str | None = ...,
    epoch_factor: float | None = ...,
    learning_rate_factor: float | None = ...,
    extra_args: Sequence[str] = ...,
) -> tuple[str, ...]: ...


def build_inference_command(
    *,
    checkpoint: str,
    python_executable: str = ...,
    script: str = ...,
    data: str = ...,
    output: str = ...,
    batch_size: int | None = ...,
    cpu: bool = ...,
    mixed_precision: str | None = ...,
    component_id_columns: Sequence[str] = ...,
    extra_args: Sequence[str] = ...,
) -> tuple[str, ...]: ...
