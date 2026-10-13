"""Expand shared parameters and task differences for existing TOML workers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from tricycle_core.errors import InvalidRequestError


def expand_task_specs(
    defaults: Mapping[str, Any], specs: Sequence[Mapping[str, Any]]
) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(defaults, Mapping) or "name" in defaults:
        raise InvalidRequestError("task_defaults must be a table without a task name")
    names, tasks = set(), []
    for row in specs:
        if not isinstance(row, Mapping):
            raise InvalidRequestError("task_specs must contain tables")
        name = row.get("name")
        if (
            not isinstance(name, str)
            or not name
            or PurePosixPath(name).name != name
            or name in {".", ".."}
            or "\\" in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            raise InvalidRequestError("task name must be a safe basename")
        if name in names:
            raise InvalidRequestError(f"Duplicate task name: {name}")
        names.add(name)
        differences = {key: value for key, value in row.items() if key != "name"}
        if defaults.keys() & differences.keys():
            raise InvalidRequestError(
                "Task differences must not repeat or override shared parameters"
            )
        tasks.append((name, {**defaults, **differences}))
    return tasks
