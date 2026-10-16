"""Resolve and verify compact replay input records without uploading them."""
from __future__ import annotations

import json
from pathlib import Path

from tricycle_core import record_io as rio
from tricycle_core.asset_store import input_paths
from tricycle_core.record_io import file_sha256


def input_file(current: Path, name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Unsafe round input path")
    local = current / "input" / relative
    if rio.is_file(local):
        if not rio.resolve(local).is_relative_to(rio.resolve(current / "input")):
            raise ValueError("Round input escapes its directory")
        return local
    paths, _ = input_paths(current / "input-assets.json")
    if name not in paths:
        raise ValueError(f"Missing round input: {name}")
    return paths[name]


def verify_inputs(current: Path) -> None:
    manifest = current / "input-assets.json"
    if rio.exists(manifest):
        expected = json.loads(rio.read_text(current / "input-assets-hash.json"))["sha256"]
        if file_sha256(manifest) != expected:
            raise ValueError("Frozen input asset references changed")
    request = json.loads(rio.read_text(input_file(current, "request.json")))
    # Resolve each shared tree once, then verify the union against worker hashes.
    paths = {p.relative_to(current / "input").as_posix(): p
             for p in rio.rglob(current / "input", "*") if rio.is_file(p)}
    if rio.exists(manifest):
        shared, _ = input_paths(manifest)
        if paths.keys() & shared.keys():
            raise ValueError("Overlapping round inputs")
        paths.update(shared)
    for name, checksum in json.loads(rio.read_text(current / "input-hashes.json")).items() if rio.exists(current / "input-hashes.json") else []:
        if name not in paths or file_sha256(paths[name]) != checksum:
            raise ValueError(f"Frozen round input changed or missing: {name}")
    for name, checksum in request["input_hashes"].items():
        input_path = paths.get(name)
        if input_path is None or file_sha256(input_path) != checksum:
            raise ValueError(f"Frozen round input changed or missing: {name}")
