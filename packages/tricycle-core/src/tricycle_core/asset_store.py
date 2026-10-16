"""Local immutable content-addressed artifacts, shared across execution records.

Mutable sources are copied before publication. Tree views share read-only blob
inodes; callers store references, never mutate views, and verify before use.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import record_io as rio
from .io import file_sha256, write_json
from .transfer import validate_transfer_file, validate_transfer_path, validate_transfer_tree


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Invalid asset SHA256")
    return value


def _relative(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in {"..", "."} for p in path.parts):
        raise ValueError(f"Unsafe asset path: {value}")
    return Path(path)


def put_file(source: Path, store: Path) -> dict[str, Any]:
    validate_transfer_file(source)
    store = store.resolve()
    checksum = file_sha256(source)
    target = store / "blobs" / checksum[:2] / checksum
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        fd, temporary = tempfile.mkstemp(dir=target.parent)
        os.close(fd)
        temp = Path(temporary)
        try:
            shutil.copyfile(source, temp)
            if file_sha256(temp) != checksum:
                raise ValueError("Asset source changed during publication")
            temp.chmod(0o444)
            try:
                os.link(temp, target)
            except FileExistsError:
                pass
            except OSError:
                # Both publishers have verified the same immutable bytes.
                if not target.exists():
                    temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
    ref = {"store": str(store), "sha256": checksum, "bytes": source.stat().st_size}
    resolve_file(ref)
    return ref


def resolve_file(reference: dict[str, Any]) -> Path:
    checksum = _identifier(reference["sha256"])
    path = Path(reference["store"]) / "blobs" / checksum[:2] / checksum
    if path.stat().st_size != reference["bytes"] or file_sha256(path) != checksum:
        raise ValueError(f"Shared asset changed: {checksum}")
    return path


def put_tree(source: Path, store: Path, *, prefix: str = "") -> dict[str, Any]:
    if not source.is_dir():
        raise ValueError(f"Asset tree source is not a directory: {source}")
    validate_transfer_path(source.name)
    paths = [p for p in sorted(source.rglob("*"))
             if "__pycache__" not in p.relative_to(source).parts and p.suffix != ".pyc"]
    for path in paths:
        validate_transfer_path(path.relative_to(source).as_posix())
        if path.is_symlink() and path.is_dir():
            raise ValueError(f"Asset tree source contains a directory symlink: {path}")
    if prefix:
        validate_transfer_path(prefix)
    store = store.resolve()
    files = {
        p.relative_to(source).as_posix(): {
            **{k: v for k, v in put_file(p, store).items() if k != "store"},
            "executable": bool(p.stat().st_mode & 0o111),
        }
        for p in paths if p.is_file()
    }
    directories = [p.relative_to(source).as_posix() for p in paths if p.is_dir()]
    if prefix:
        base = _relative(prefix)
        files = {(base / name).as_posix(): record for name, record in files.items()}
        directories = sorted({prefix, *(p.as_posix() for p in base.parents if p != Path(".")),
                              *((base / name).as_posix() for name in directories)})
    contents = {"files": files, "directories": directories}
    payload = json.dumps(contents, sort_keys=True, separators=(",", ":")).encode()
    checksum = hashlib.sha256(payload).hexdigest()
    target = store / "trees" / checksum
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = store / "manifests" / f"{checksum}.json"
    # Concurrent publishers produce exactly the same content.
    if not target.exists():
        with tempfile.TemporaryDirectory(dir=target.parent) as temp:
            staging = Path(temp) / "tree"
            staging.mkdir()
            for name in directories:
                (staging / _relative(name)).mkdir(parents=True, exist_ok=True)
            for name, record in files.items():
                destination = staging / _relative(name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                blob = resolve_file({"store": str(store), **record})
                if record["executable"]:
                    shutil.copyfile(blob, destination)
                    destination.chmod(0o555)
                else:
                    try:
                        os.link(blob, destination)
                    except OSError:
                        shutil.copy2(blob, destination)
            try:
                staging.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
    if not manifest.exists():
        write_json(manifest, contents)
    reference = {"schema": "tricycle.asset-tree/v2", "store": str(store), "sha256": checksum}
    resolve_tree(reference)
    return reference


def resolve_tree(reference: dict[str, Any]) -> Path:
    if reference.get("schema") not in {"tricycle.asset-tree/v1", "tricycle.asset-tree/v2"}:
        raise ValueError("Unsupported asset tree reference")
    checksum = _identifier(reference["sha256"])
    store = Path(reference["store"])
    contents = json.loads((store / "manifests" / f"{checksum}.json").read_text())
    payload = json.dumps(contents, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(payload).hexdigest() != checksum:
        raise ValueError("Asset tree manifest changed")
    root = store / "trees" / checksum
    files = contents if reference["schema"] == "tricycle.asset-tree/v1" else contents["files"]
    if reference["schema"] == "tricycle.asset-tree/v2":
        directories = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_dir()}
        if directories != set(contents["directories"]):
            raise ValueError("Asset tree directory set changed")
    if any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError("Asset tree escapes store")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual != set(files):
        raise ValueError("Asset tree file set changed")
    for name, record in files.items():
        path = root / _relative(name)
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Asset tree escapes store")
        if file_sha256(path) != record["sha256"] or path.stat().st_size != record["bytes"]:
            raise ValueError(f"Shared asset tree changed: {name}")
    return root


def externalize_tree(directory: Path, store: Path) -> dict[str, Any]:
    """Replace a prepared tree with a reference view, preserving all file bytes."""
    reference = put_tree(directory, store)
    target = resolve_tree(reference)
    shutil.rmtree(directory)
    directory.symlink_to(target, target_is_directory=True)
    return reference


def materialize_directory(source: Path, destination: Path) -> None:
    """Dereference a prepared view into an independent writable execution tree."""
    validate_transfer_tree(source)
    shutil.copytree(source, destination)
    for path in destination.rglob("*"):
        if path.is_file():
            path.chmod(path.stat().st_mode | 0o200)


def input_paths(manifest: Path) -> tuple[dict[str, Path], set[str]]:
    """Verify a delivery manifest and resolve its files without a run-local view."""
    value = json.loads(rio.read_text(manifest))
    schema = value.get("schema")
    if schema not in {"tricycle.asset-input/v1", "tricycle.asset-input/v2"}:
        raise ValueError("Unsupported asset input manifest")
    files: dict[str, Path] = {}
    directories: set[str] = set()
    trees = value.get("trees", []) if schema.endswith("/v1") else value.get("trees", {})
    entries = (("", ref) for ref in trees) if isinstance(trees, list) else trees.items()
    for prefix, reference in entries:
        base = _relative(prefix) if prefix else Path()
        root = resolve_tree(reference)
        if reference.get("path"):
            root = root / _relative(reference["path"])
        if not root.is_dir():
            raise ValueError("Asset tree input path is not a directory")
        if prefix:
            directories.add(base.as_posix())
        for path in root.rglob("*"):
            name = (base / path.relative_to(root)).as_posix()
            if path.is_dir():
                directories.add(name)
            elif path.is_file():
                if name in files:
                    raise ValueError("Overlapping asset input trees")
                files[name] = path
    for name, reference in value.get("files", {}).items():
        name = _relative(name).as_posix()
        if name in files:
            raise ValueError("Overlapping asset input files")
        files[name] = resolve_file(reference)
    for name in files:
        parents = {p.as_posix() for p in Path(name).parents if p != Path(".")}
        if name in directories or parents.intersection(files):
            raise ValueError("Asset input file/directory collision")
    return files, directories


def materialize_input(manifest: Path, overlay: Path | None, destination: Path) -> None:
    """Expand verified references into an independent, disposable upload tree."""
    files, directories = input_paths(manifest)
    for name in (*files, *directories):
        validate_transfer_path(name)
    executable_files = {name for name, ref in json.loads(rio.read_text(manifest)).get("files", {}).items()
                        if ref.get("executable", False)}
    if overlay is not None:
        validate_transfer_tree(overlay)
        for path in overlay.rglob("*"):
            name = path.relative_to(overlay).as_posix()
            if path.is_symlink():
                raise ValueError("Asset overlay collision or symlink")
            if path.is_dir():
                directories.add(name)
            elif path.is_file():
                if name in files:
                    raise ValueError("Asset overlay collision or symlink")
                files[name] = path
    for source in files.values():
        validate_transfer_file(source)
    destination.mkdir(parents=True, exist_ok=False)
    for name in sorted(directories):
        (destination / _relative(name)).mkdir(parents=True, exist_ok=True)
    for name, source in files.items():
        target = destination / _relative(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755 if name in executable_files or source.stat().st_mode & 0o111 else 0o644)


def freeze_inputs(directory: Path, paths: tuple[Path, ...], store: Path) -> None:
    """Publish input references before removing their run-local entities."""
    manifest = directory / "input-assets.json"
    value: dict[str, Any] = json.loads(rio.read_text(manifest)) if manifest.exists() else {
        "schema": "tricycle.asset-input/v2", "trees": {}, "files": {},
    }
    if value.get("schema") != "tricycle.asset-input/v2":
        raise ValueError("Unsupported input record schema")
    names = [path.relative_to(directory).as_posix() for path in paths]
    bound = [*value["trees"], *value["files"]]
    for index, name in enumerate(names):
        relative = _relative(name)
        if name == "input-assets.json" or any(
            relative.is_relative_to(_relative(other)) or _relative(other).is_relative_to(relative)
            for other in [*bound, *names[:index]]
        ):
            raise ValueError("Overlapping input record paths")
    for path in paths:
        name = path.relative_to(directory).as_posix()
        _relative(name)
        if path.is_dir() and store.resolve().is_relative_to(path.resolve()):
            raise ValueError("Asset store must be outside the published input")
        if path.is_dir():
            value["trees"][name] = {**put_tree(path, store, prefix=path.name), "path": path.name}
        else:
            value["files"][name] = {**put_file(path, store), "executable": bool(path.stat().st_mode & 0o111)}
    write_json(manifest, value)
    input_paths(manifest)
    for path in paths:
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)


def resolve_input(directory: Path, name: str) -> Path:
    """Read frozen inputs; retain read access to existing physical run records."""
    relative = _relative(name)
    manifest = directory / "input-assets.json"
    if manifest.exists():
        value = json.loads(rio.read_text(manifest))
        if value.get("schema") != "tricycle.asset-input/v2":
            raise ValueError("Unsupported input record schema")
        if name in value["files"]:
            return resolve_file(value["files"][name])
        for prefix, ref in value["trees"].items():
            base = _relative(prefix)
            if relative.is_relative_to(base):
                root = resolve_tree(ref)
                if ref.get("path"):
                    root = root / _relative(ref["path"])
                return root / relative.relative_to(base)
    return directory / relative
