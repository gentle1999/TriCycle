"""Publish SAR input references separately from execution and result records."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from tricycle_core.asset_store import (
    freeze_inputs,
    materialize_directory,
    materialize_input,
    resolve_input,
    resolve_tree,
)
from tricycle_core.toml import dump_toml, load_toml


def freeze(directory: Path, folders: tuple[Path, ...], *, store: Path | None = None) -> None:
    """Remove input entities and bind prepared requests to verified shared assets."""
    store = (store or Path.cwd() / "artifacts/task-assets").resolve()
    originals = [(str(folder.resolve()), folder.relative_to(directory).as_posix()) for folder in folders]
    freeze_inputs(directory, folders, store)
    replacements = [(old, str(resolve_input(directory, name))) for old, name in originals]
    # Only rewrite storage paths in generated submission requests, never protocol values.
    for request in directory.glob("bohr-*.toml"):
        value = load_toml(request)
        for field in ("script", "worker_script"):
            if field in value:
                value[field] = _input_path(value[field], replacements)
        for field in ("common_paths", "task_patterns"):
            if field in value:
                value[field] = [_input_path(path, replacements) for path in value[field]]
        dump_toml(request, value)


def _input_path(path: str, replacements: list[tuple[str, str]]) -> str:
    for original, target in replacements:
        if path == original or path.startswith(original + "/"):
            return target + path[len(original):]
    return path


def materialize(directory: Path, destination: Path) -> None:
    """Create an independent execution tree from references and task records."""
    if not (directory / "input-assets.json").exists():
        # Existing records retain their original, hash-checked directory views.
        records = json.loads((directory / "asset-references.json").read_text())
        for name, reference in records.items():
            if resolve_tree(reference) != (directory / name).resolve():
                raise ValueError("SAR asset reference differs")
        materialize_directory(directory, destination)
        return
    materialize_input(directory / "input-assets.json", None, destination)
    # Task descriptors are run records; copy only the explicitly supported worker entries.
    for name in ("task.toml", "tasks", "worker.sh"):
        source = directory / name
        if source.exists() and (destination / name).exists():
            raise ValueError(f"Run record shadows a frozen worker input: {name}")
        if source.is_dir():
            shutil.copytree(source, destination / name)
        elif source.is_file():
            shutil.copy2(source, destination / name)
