"""Static interface for the accompanying runtime implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd  # pyright: ignore[reportMissingImports]

from ..workflow_common import FoldSplit, WorkflowContext

@dataclass(frozen=True)
class ChempropParams:
    name: str
    message_hidden_dim: int
    depth: int
    dropout: float
    ffn_hidden_dim: int
    ffn_num_layers: int

    def to_manifest(self) -> dict[str, int | float | str]: ...

@dataclass(frozen=True)
class ChempropMode:
    model_name: str
    input_columns: tuple[str, ...]
    input_args: tuple[str, ...]
    description: str

    def to_manifest(self) -> dict[str, Any]: ...

CHEMPROP_CGR_MODE: Any = ...
CHEMPROP_MULTICOMPONENT_MODE: Any = ...
CHEMPROP_GROUP_MODELS: tuple[str, ...] = ...
CHEMPROP_MODES: Any = ...

def split_reaction_smiles(rxn_smiles: str) -> tuple[str, str, str]: ...
def write_chemprop_csv(
    path: Path, df: pd.DataFrame, indices: list[int], target: str, mode: ChempropMode
) -> None: ...
def chemprop_train_args_for_params(
    ctx: WorkflowContext,
    train_csv: Path,
    val_csv: Path,
    test_csv: Path,
    output_dir: Path,
    params: ChempropParams,
    mode: ChempropMode,
) -> list[str]: ...
def standard_train_args(
    ctx: WorkflowContext,
    train_csv: Path,
    val_csv: Path,
    test_csv: Path,
    output_dir: Path,
    *,
    mode: ChempropMode,
    message_hidden_dim: int = ...,
    depth: int = ...,
    dropout: float = ...,
    ffn_hidden_dim: int = ...,
    ffn_num_layers: int = ...,
) -> list[str]: ...

CHEMPROP_QUICK_HPO_CANDIDATES: Any = ...
CHEMPROP_STANDARD_HPO_CANDIDATES: Any = ...

def chemprop_hpo_candidates(profile: str) -> list[ChempropParams]: ...
def run_chemprop_outer_hpo(
    ctx: WorkflowContext,
    fold_root: Path,
    train_csv: Path,
    val_csv: Path,
    test_csv: Path,
    mode: ChempropMode,
) -> tuple[ChempropParams, list[dict[str, Any]]]: ...
def run_chemprop_fold(
    ctx: WorkflowContext, split: FoldSplit, *, hpopt: bool, mode: ChempropMode
) -> dict[str, Any]: ...
