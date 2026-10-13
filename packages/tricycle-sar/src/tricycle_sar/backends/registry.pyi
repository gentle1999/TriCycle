"""Static interface for the accompanying runtime implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

CATALOG: Any = ...

def load_registry(reference: Path, definition: dict) -> dict: ...
def render_config(bundle: Path, method: dict) -> None: ...
