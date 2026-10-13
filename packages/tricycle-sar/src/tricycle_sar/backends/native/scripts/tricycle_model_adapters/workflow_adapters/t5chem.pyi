"""Static interface for the accompanying runtime implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd  # pyright: ignore[reportMissingImports]

from ..workflow_common import FoldSplit, WorkflowContext

def write_t5chem_files(path: Path, df: pd.DataFrame, indices: list[int], target: str) -> None: ...
def write_t5chem_split_files(
    root: Path, df: pd.DataFrame, split: FoldSplit, target: str
) -> None: ...
def write_simple_vocab(path: Path, df: pd.DataFrame, target: str) -> None: ...
def run_t5chem_fold(ctx: WorkflowContext, split: FoldSplit) -> dict[str, Any]: ...
