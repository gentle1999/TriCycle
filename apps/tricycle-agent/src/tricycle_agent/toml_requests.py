from __future__ import annotations

from pathlib import Path
from typing import Any

from tricycle_core import InvalidRequestError
from tricycle_core.toml import load_toml


def load_request(path: Path) -> dict[str, Any]:
    """Load one user request and enforce the repository TOML boundary."""

    if path.suffix.lower() != ".toml":
        raise InvalidRequestError(
            "configuration requests must use a .toml file; JSON and other formats are not supported"
        )
    value = load_toml(path)
    if not isinstance(value, dict):
        raise InvalidRequestError(f"TOML request must contain a table: {path}")
    return value
