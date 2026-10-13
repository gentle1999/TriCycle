"""Stage canonical full-training methods; mutable tools are never a recipe fallback."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from tricycle_core.toml import load_toml
from tricycle_core.training_protocol import (
    file_sha256,
    protocol_file_source,
    resolve_training_registry,
    training_method,
    validate_generated_inputs,
)

from .backends.registry import CATALOG, render_config

LOCK = CATALOG
digest = file_sha256


def require_benchmark_protocol(run: Path) -> None:
    """An experiment cannot silently acquire a different protocol during preparation."""
    state_file = run / "run.toml"
    if not state_file.exists():
        return
    state = load_toml(state_file)
    expected = state.get("training_protocol_manifest_sha256")
    if expected:
        manifest, _ = resolve_training_registry(LOCK)
        if digest(manifest) != expected:
            raise ValueError("Benchmark is bound to a different training backend")


def protocol(model: str) -> tuple[dict, str]:
    manifest, registry = resolve_training_registry(LOCK)
    method_id = registry["models"][model]["default_method"]
    method = training_method(manifest, registry, model, method_id)
    return {**method, "method_id": method_id, "manifest": str(manifest)}, digest(manifest)


def protocol_file(model: str, path: str) -> Path:
    record, _ = protocol(model)
    entry = next(item for item in record["files"] if item["path"] == path)
    return protocol_file_source(Path(record["manifest"]), entry)


def stage_protocol_bundle(model: str, bundle: Path) -> None:
    """Copy the method's code/config/assets before generating data-bound task hashes."""
    record, _ = protocol(model)
    if bundle.name != record["bundle_name"]:
        raise ValueError(f"Training bundle differs from method: {model}")
    prefix = f"common/{bundle.name}/"
    declared = {
        entry["path"].removeprefix(prefix)
        for entry in record["files"]
        if entry["path"].startswith(prefix)
    }
    for path in (bundle / "scripts").rglob("*"):
        if path.is_file() and str(path.relative_to(bundle)) not in declared:
            path.unlink()
    for entry in record["files"]:
        if not entry["path"].startswith(prefix):
            continue
        target = bundle / entry["path"].removeprefix(prefix)
        if entry["external"]:
            if not target.is_file() or digest(target) != entry["sha256"]:
                raise ValueError(f"Fixed protocol asset missing or changed: {entry['path']}")
            continue
        source = protocol_file_source(Path(record["manifest"]), entry)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755 if entry["executable"] else 0o644)
    render_config(bundle, record)


def apply_protocol(model: str, request: dict, directory: Path, bundle: Path) -> None:
    record, sha = protocol(model)
    if record["executor"] != "bohr":
        raise ValueError(f"{model} requires its pinned internal executor")
    if request["image"] != record["image"] or request["machine_type"] != record["machine_type"]:
        raise ValueError(f"Training protocol executor differs: {model}")
    validate_generated_inputs(directory, record, remove_prefix="common/")
    runtime = directory / "protocol-runtime"
    runtime.mkdir(exist_ok=True)
    prefix = f"common/{bundle.name}/"
    for entry in record["files"]:
        name = entry["path"]
        if name.startswith(prefix):
            target = bundle / name.removeprefix(prefix)
            if not target.is_file() or digest(target) != entry["sha256"]:
                raise ValueError(f"Staged training method differs: {name}")
        else:
            relative = name.removeprefix("common/")
            target = runtime / ("worker.sh" if relative == "__bohr_task_script.sh" else relative)
            if entry["external"]:
                raise ValueError(f"External executor asset must be supplied: {name}")
            source = protocol_file_source(Path(record["manifest"]), entry)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and digest(target) != entry["sha256"]:
                raise ValueError(f"Staged training protocol changed: {name}")
            if not target.exists():
                shutil.copyfile(source, target)
            target.chmod(0o755 if entry["executable"] else 0o644)
    request["script"] = str((runtime / "worker.sh").resolve())
    runtime_names = {p.name for p in runtime.iterdir()} | {"sar_model_envs"}
    request["common_paths"] = [
        p
        for p in request["common_paths"]
        if Path(p).name not in runtime_names and Path(p).name != "training-protocol.json"
    ]
    request["common_paths"] += [
        str(p.resolve()) for p in runtime.iterdir() if p.name != "worker.sh"
    ]
    marker = directory / "training-protocol.json"
    binding = {
        "model": model,
        "registry": record["manifest"],
        "registry_sha256": sha,
        "method_id": record["method_id"],
    }
    if marker.exists():
        if json.loads(marker.read_text()) != binding:
            raise ValueError("Staged training protocol binding differs")
    else:
        marker.write_text(json.dumps(binding, indent=2) + "\n")
    request["common_paths"].append(str(marker.resolve()))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model")
    args = parser.parse_args()
    manifest, registry = resolve_training_registry(LOCK)
    checked = []
    for method_id, method in registry["methods"].items():
        if args.model is None or method["model"] == args.model:
            training_method(manifest, registry, method["model"], method_id)
            checked.append(method_id)
    if not checked:
        parser.error("unknown model")
    print(
        json.dumps(
            {
                "status": "verified",
                "manifest": str(manifest),
                "manifest_sha256": digest(manifest),
                "methods": checked,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
