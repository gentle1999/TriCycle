"""Shared atomic artifacts, file fingerprints and independent source snapshots."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def write_json(path: Path, value: object) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )


def write_delimited(
    path: Path,
    fields: Sequence[str],
    rows: Sequence[Mapping[str, object]],
    *,
    delimiter: str = ",",
    project_fields: bool = True,
) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=list(fields), delimiter=delimiter, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(
        ({key: row.get(key, "") for key in fields} for row in rows) if project_fields else rows
    )
    atomic_write_text(path, buffer.getvalue())


def file_sha256(path: Path) -> str:
    with Path(path).open("rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


def tree_sha256(
    root: Path,
    *,
    excluded: frozenset[str] = frozenset({".git", "__pycache__", ".pytest_cache", ".venv"}),
) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file() or any(part in excluded for part in path.relative_to(root).parts):
            continue
        name = path.relative_to(root).as_posix().encode()
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(bytes.fromhex(file_sha256(path)))
    return digest.hexdigest()


def copy_snapshot_file(source: str | Path, destination: str | Path) -> None:
    """Mutable source files must never share an inode with their frozen copy."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def copy_snapshot_tree(source: Path, destination: Path, *, dirs_exist_ok: bool = False) -> None:
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=dirs_exist_ok,
        copy_function=copy_snapshot_file,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", ".cache", "__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache"
        ),
    )
