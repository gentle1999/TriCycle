from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tricycle_core.toml import dump_toml, load_toml

__all__ = ["dump_toml", "flatten_sections", "load_toml"]


def flatten_sections(value: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten one level of run TOML sections for legacy-shaped consumers."""

    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            result.update(item)
        else:
            result[key] = item
    return result
