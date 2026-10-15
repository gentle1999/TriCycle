"""Validate an injected training protocol before any remote submission side effect."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tricycle_core.asset_store import resolve_tree
from tricycle_core.errors import InvalidRequestError
from tricycle_core.training_protocol import (
    file_sha256,
    resolve_training_registry,
    training_method,
    validate_generated_inputs,
    verify_asset_files,
)

from .config import dump_toml, load_toml

_digest = file_sha256


def worker_input(run: Path, worker: str, config: dict | None = None) -> Path:
    """Resolve current reference records or existing physical worker inputs."""
    if Path(worker).name != worker or worker in {".", ".."}:
        raise ValueError("Unsafe worker name")
    if config is None:
        config = load_toml(run / "batch.toml")
    reference = config.get("input_assets", {}).get(worker)
    manifest = run / worker / "input-assets.json"
    if reference is not None:
        value = json.loads(manifest.read_text())
        if value != {"schema": "tricycle.asset-input/v1", "trees": [reference], "files": {}}:
            raise ValueError("Worker input manifest differs from batch record")
        return resolve_tree(reference)
    if manifest.exists():
        raise ValueError("Worker input manifest has no batch binding")
    return run / worker


def bind_staged_training_protocol(run: Path, binding: dict, config: dict) -> None:
    """Record one method reference and one copy of shared experiment hashes."""
    manifest, registry = resolve_training_registry(
        Path(binding["registry"]), binding["registry_sha256"]
    )
    method = training_method(manifest, registry, binding["model"], binding["method_id"])
    workers = [worker_input(run, path.name, config) for path in sorted(run.glob("worker-*"))]
    owned = {entry["path"] for entry in method["files"]}
    common_files = {
        path.relative_to(workers[0]).as_posix(): _digest(path)
        for path in sorted((workers[0] / "common").rglob("*"))
        if path.is_file() and path.relative_to(workers[0]).as_posix() not in owned
    }
    files = {
        f"{record.name}/{path.relative_to(worker).as_posix()}": _digest(path)
        for record, worker in zip(sorted(run.glob("worker-*")), workers, strict=True)
        for path in sorted(worker.rglob("*"))
        if path.is_file() and not path.is_relative_to(worker / "common")
    }
    for name in ("submit-plan.tsv", "tasks.tsv"):
        path = run / name
        if path.is_file():
            files[name] = _digest(path)
    marker = run / "training-protocol.json"
    if marker.exists():
        raise FileExistsError(marker)
    marker.write_text(
        json.dumps(
            {
                **{
                    key: binding[key]
                    for key in ("model", "registry", "registry_sha256", "method_id")
                },
                "input_assets": config.get("input_assets", {}),
                "workers": [worker.name for worker in sorted(run.glob("worker-*"))],
                "common_files": common_files,
                "files": files,
            },
            indent=2,
        )
        + "\n"
    )
    config["run"]["training_protocol_required"] = True
    config["run"]["training_protocol_sha256"] = _digest(marker)
    dump_toml(run / "batch.toml", config)
    validate_training_protocol(run)


def _validate_reactiont5_assets(bundle: Path, digest: Any = _digest) -> None:
    """Reject missing weights before a worker can fall back to HF."""
    spec = json.loads((bundle / "reactiont5.json").read_text())
    if not any(e["path"] == "pytorch_model.bin" and "sha256" in e for e in spec["files"]):
        raise ValueError("ReactionT5 requires a pinned local checkpoint hash")
    try:
        missing = verify_asset_files(bundle / "assets", spec["files"], digest=digest)
    except ValueError as exc:
        raise ValueError(f"ReactionT5 local asset hash differs: {exc}") from exc
    if missing:
        raise ValueError(f"ReactionT5 local asset missing or unsafe: {missing}")


def validate_training_protocol(run: Path) -> None:
    checked = {}

    def digest(path: Path) -> str:
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if identity not in checked:
            checked[identity] = _digest(path)
        return checked[identity]

    def validate_files(base: Path, files: dict, label: str) -> None:
        for name, expected in files.items():
            target = (base / name).resolve()
            allowed = (base / "common").resolve() if name.startswith("common/") else base.resolve()
            if not target.is_relative_to(allowed) or digest(target) != expected:
                raise ValueError(f"{label}: {name}")

    marker = run / "training-protocol.json"
    config = load_toml(run / "batch.toml") if (run / "batch.toml").exists() else {}
    try:
        references = config.get("input_assets", {})
        if references and set(references) != {p.name for p in run.glob("worker-*") if p.is_dir()}:
            raise ValueError("Worker input reference set differs")
        for worker in references:
            worker_input(run, worker, config)
        for worker, ref in config.get("shared_assets", {}).items():
            if Path(worker).name != worker or resolve_tree(ref) != (run / worker / "common").resolve():
                raise ValueError("Shared worker input assets differ")
    except (KeyError, OSError, ValueError) as exc:
        raise InvalidRequestError(f"Shared worker input assets rejected: {exc}") from exc
    required = config.get("run", {}).get("training_protocol_required", False)
    if list(run.glob("worker-*/common/training-protocol.json")):
        raise InvalidRequestError("Per-worker training protocol records are unsupported; prepare a new batch")
    if required and not marker.is_file():
        raise InvalidRequestError("Required training protocol is missing; submission rejected")
    if not marker.exists():
        return
    expected_marker = config.get("run", {}).get("training_protocol_sha256")
    if not expected_marker or _digest(marker) != expected_marker:
        raise InvalidRequestError("Prepared training protocol marker changed or unbound; submission rejected")
    try:
        data = json.loads(marker.read_text())
        registry_path, registry = resolve_training_registry(
            Path(data["registry"]), data["registry_sha256"]
        )
        method = training_method(registry_path, registry, data["model"], data["method_id"])
        if method["executor"] != "bohr":
            raise ValueError("Training method requires its pinned internal executor")
        for field in ("image", "machine_type"):
            if config.get("bohr", {}).get(field) != method[field]:
                raise ValueError(f"Protocol executor differs: {field}")
        expected_method = {entry["path"]: entry["sha256"] for entry in method["files"]}
        worker_bases = sorted(run.glob("worker-*"))
        if not worker_bases or data["workers"] != [worker.name for worker in worker_bases]:
            raise ValueError("Staged training worker set differs")
        if data.get("input_assets", {}) != config.get("input_assets", {}):
            raise ValueError("Worker input references differ from protocol")
        for record in worker_bases:
            worker = worker_input(run, record.name, config)
            reference = data.get("shared_assets", {}).get(record.name)
            if reference is not None and resolve_tree(reference) != (worker / "common").resolve():
                raise ValueError("Shared SAR assets differ")
            if (worker / "common").is_symlink() and reference is None:
                raise ValueError("Unbound shared SAR assets")
            validate_files(worker, expected_method, "Prepared training method differs")
            validate_generated_inputs(worker / "common", method, digest=digest, remove_prefix="common/")
            validate_files(worker, data["common_files"], "Prepared shared training input differs")
        for name, expected in data["files"].items():
            parts = Path(name).parts
            if len(parts) > 1 and parts[0] in data["workers"]:
                validate_files(worker_input(run, parts[0], config),
                               {Path(*parts[1:]).as_posix(): expected}, "Prepared training input differs")
            else:
                validate_files(run, {name: expected}, "Prepared training input differs")
        if data["model"] == "reactiont5":
            for worker in worker_bases:
                _validate_reactiont5_assets(worker_input(run, worker.name, config) / "common/training_bundle", digest)
    except (KeyError, OSError, ValueError) as exc:
        raise InvalidRequestError(
            f"Training protocol rejected submission: {marker}: {exc}"
        ) from exc
