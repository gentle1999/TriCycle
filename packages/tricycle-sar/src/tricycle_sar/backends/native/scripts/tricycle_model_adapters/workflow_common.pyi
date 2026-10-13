"""Static interface for the accompanying runtime implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd  # pyright: ignore[reportMissingImports]

REACTION_COLUMN: str = ...
DEFAULT_TARGET: str = ...
WORKFLOW_RUN_SCHEMA_VERSION: str = ...
DEFAULT_DOCKER_ENV: Any = ...

class WorkflowRunError(RuntimeError): ...

@dataclass(frozen=True)
class FoldSplit:
    fold: int
    train: list[int]
    val: list[int]
    test: list[int]

@dataclass(frozen=True)
class WorkflowTask:
    model: str
    fold: FoldSplit
    gpu_device: str | None

@dataclass(frozen=True)
class WorkflowContext:
    workspace: Path
    snapshot: Path
    split_manifest: Path
    dataset: pd.DataFrame
    output: Path
    target: str
    seed: int
    timeout_seconds: int
    gpu_device: str | None
    reamvp_gpu: bool
    profile: str
    dry_run: bool

    def container_path(self, host_path: Path) -> str: ...

def profile_epochs(ctx: WorkflowContext) -> int: ...
def profile_warmup_epochs(ctx: WorkflowContext) -> int: ...
def profile_num_workers(ctx: WorkflowContext) -> int: ...
def utc_now() -> str: ...
def run_command(
    command: list[str],
    *,
    log_path: Path,
    cwd: Path,
    timeout_seconds: int,
    dry_run: bool,
    env: dict[str, str] | None = ...,
) -> dict[str, Any]: ...
def gpu_args(ctx: WorkflowContext, *, use_gpu: bool) -> list[str]: ...
def docker_env_args(env: dict[str, str] | None = ...) -> list[str]: ...
def docker_command(
    ctx: WorkflowContext, image: str, args: list[str], *, workdir: str = ..., use_gpu: bool = ...
) -> list[str]: ...
def docker_command_with_env(
    ctx: WorkflowContext,
    image: str,
    args: list[str],
    *,
    env: dict[str, str],
    workdir: str = ...,
    use_gpu: bool = ...,
) -> list[str]: ...
def load_snapshot_dataset(snapshot: Path) -> tuple[Path, pd.DataFrame]: ...
def load_kfold_splits(split_manifest: Path, *, max_folds: int | None = ...) -> list[FoldSplit]: ...
def write_normalized_predictions(
    path: Path, df: pd.DataFrame, indices: list[int], target: str, y_pred: list[float]
) -> dict[str, Any]: ...
def compute_metrics_from_predictions(predictions: Path, target: str) -> dict[str, Any]: ...
def read_first_numeric_column(path: Path, *, preferred: str | None = ...) -> list[float]: ...
def write_toml(path: Path, sections: dict[str, dict[str, object]]) -> None: ...
def summarize_model(results: list[dict[str, Any]]) -> dict[str, Any]: ...
