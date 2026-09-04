from __future__ import annotations

import os
import tempfile
import tomllib
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from .errors import InvalidRequestError


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
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _value(value: object) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Path):
        return _quote(str(value))
    if isinstance(value, str):
        return _quote(value)
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
            nested.append(((*prefix, key), value))
        else:
            scalar_lines.append(f"{key} = {_value(value)}")
    return scalar_lines, nested


def dump_toml(path: Path, mapping: Mapping[str, object]) -> None:
    """Write the deterministic TOML subset used by TriCycle run metadata."""

    lines: list[str] = []
    root_scalars, root_sections = _sections(mapping)
    lines.extend(root_scalars)
    for section_path, section in root_sections:
        if lines:
            lines.append("")
        lines.append("[" + ".".join(section_path) + "]")
        scalar_lines, nested = _sections(section, section_path)
        lines.extend(scalar_lines)
        pending = nested
        while pending:
            next_pending: list[tuple[tuple[str, ...], Mapping[str, object]]] = []
            for child_path, child in pending:
                lines.append("")
                lines.append("[" + ".".join(child_path) + "]")
                child_scalars, child_nested = _sections(child, child_path)
                lines.extend(child_scalars)
                next_pending.extend(child_nested)
            pending = next_pending
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_path)
