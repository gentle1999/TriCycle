from __future__ import annotations

import json
import math
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import InvalidRequestError
from .io import atomic_write_text


def load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise InvalidRequestError(f"TOML file not found: {path}")
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise InvalidRequestError(f"invalid TOML configuration: {path}") from exc
    return value


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else _quote(value)


def _value(value: object) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidRequestError("TOML does not support non-finite floats")
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Path):
        return _quote(str(value))
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, Mapping):
        return (
            "{"
            + ", ".join(f"{_quote(str(key))} = {_value(item)}" for key, item in value.items())
            + "}"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise InvalidRequestError(f"unsupported TOML value: {value!r}")


def _sections(
    mapping: Mapping[str, object], prefix: tuple[str, ...] = ()
) -> tuple[list[str], list[tuple[tuple[str, ...], Mapping[str, object]]]]:
    scalar_lines: list[str] = []
    nested: list[tuple[tuple[str, ...], Mapping[str, object]]] = []
    for key, value in mapping.items():
        if isinstance(value, Mapping):
            nested.append(((*prefix, str(key)), value))
        else:
            scalar_lines.append(f"{_key(str(key))} = {_value(value)}")
    return scalar_lines, nested


def render_toml(mapping: Mapping[str, object]) -> str:
    """Render the deterministic TOML subset without requiring a filesystem file."""

    lines: list[str] = []
    root_scalars, root_sections = _sections(mapping)
    lines.extend(root_scalars)
    for section_path, section in root_sections:
        if lines:
            lines.append("")
        lines.append("[" + ".".join(_key(part) for part in section_path) + "]")
        scalar_lines, nested = _sections(section, section_path)
        lines.extend(scalar_lines)
        pending = nested
        while pending:
            next_pending: list[tuple[tuple[str, ...], Mapping[str, object]]] = []
            for child_path, child in pending:
                lines.append("")
                lines.append("[" + ".".join(_key(part) for part in child_path) + "]")
                child_scalars, child_nested = _sections(child, child_path)
                lines.extend(child_scalars)
                next_pending.extend(child_nested)
            pending = next_pending
    return "\n".join(lines) + "\n"


def dump_toml(path: Path, mapping: Mapping[str, object]) -> None:
    """Write deterministic TOML to an ordinary filesystem destination."""
    atomic_write_text(path, render_toml(mapping))
