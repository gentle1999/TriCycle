"""Shared transfer boundaries for task inputs and result artifacts."""
from __future__ import annotations

import filecmp
import os
import re
import shutil
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from .errors import InvalidRequestError

_RUNTIME_PARTS = frozenset({
    ".venv", "venv", ".virtualenv", ".conda", "conda-meta", "site-packages",
    "dist-packages", ".cache", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".pip-cache", ".uv-cache", ".tox", ".nox", ".git", "pyvenv.cfg",
})


def validate_transfer_path(name: str) -> None:
    """Reject installed environments and caches, including renamed venv markers."""
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if any(part.lower() in _RUNTIME_PARTS for part in parts) or name.lower().endswith((".pyc", ".pyo")):
        raise InvalidRequestError(
            f"Runtime environment/cache is not a task artifact: {name}. "
            "Prepare a clean bundle with source, dependency declarations, inputs, and results."
        )


def validate_result_selector(value: str) -> str:
    """Allow named result directories and file globs, never whole-workspace globs."""
    selector = value.strip().replace("\\", "/")
    while selector.startswith("./"):
        selector = selector[2:]
    parts = selector.rstrip("/").split("/")
    if (not selector or selector.startswith("/") or any(p in {"", ".", ".."} for p in parts)
            or "\x00" in selector):
        raise InvalidRequestError("Result selectors must be non-empty relative artifact paths")
    validate_transfer_path(selector)
    if any(re.search(r"[*?\[]", part) for part in parts[:-1]) or (
        re.search(r"[*?\[]", parts[-1])
        and (selector.endswith("/") or "**" in parts[-1]
             or not re.search(r"\.[A-Za-z0-9_-]+$", parts[-1]))
    ):
        raise InvalidRequestError(f"Overbroad result selector: {value}; name result directories or file types")
    return selector


def validate_transfer_file(path: Path) -> None:
    """Inspect ZIP/tar member names without extracting or rewriting source assets."""
    validate_transfer_path(path.name)
    lower = path.name.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                validate_transfer_path(member.filename)
    elif lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        with tarfile.open(path) as archive:
            for tar_member in archive:
                validate_transfer_path(tar_member.name)
                if tar_member.issym() or tar_member.islnk():
                    validate_transfer_path(tar_member.linkname)


def validate_transfer_tree(root: Path) -> None:
    """Check the actual delivery tree, following shared views but rejecting cycles."""
    validate_transfer_path(root.name)
    if not root.is_dir():
        raise InvalidRequestError(f"Transfer directory does not exist: {root}")
    for directory, dirs, files in os.walk(root, followlinks=True):
        path = Path(directory)
        resolved = path.resolve()
        for parent in path.parents:
            if parent == root.parent:
                break
            if parent.resolve() == resolved:
                raise InvalidRequestError(f"Cyclic directory in transfer: {path}")
        for name in (*dirs, *files):
            child = path / name
            validate_transfer_path(child.relative_to(root).as_posix())
            if child.is_symlink() and not child.exists():
                raise InvalidRequestError(f"Broken symlink in transfer: {child}")
        for name in files:
            validate_transfer_file(path / name)


def publish_download(staging: Path, destination: Path) -> tuple[str, ...]:
    """Validate disposable downloads before publishing; never overwrite old evidence."""
    validate_transfer_tree(staging)
    if destination.is_symlink():
        raise InvalidRequestError(f"Download destination is a symlink: {destination}")
    destination = destination.resolve()
    files = [path for path in staging.rglob("*") if path.is_file()]
    for path in staging.rglob("*"):
        target = destination / path.relative_to(staging)
        if path.is_symlink() or target.is_symlink() or (target.exists() and (
            path.is_dir() != target.is_dir() or (
                path.is_file() and not filecmp.cmp(path, target, shallow=False))
        )):
            raise InvalidRequestError(f"Download would replace existing evidence: {target}")
    destination.mkdir(parents=True, exist_ok=True)
    for path in staging.rglob("*"):
        target = destination / path.relative_to(staging)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return tuple(str(destination / path.relative_to(staging)) for path in sorted(files))
