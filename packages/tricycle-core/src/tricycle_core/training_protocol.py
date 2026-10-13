"""Resolve current training methods and verify their local inputs without network access."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from pathlib import Path, PurePosixPath

from .io import file_sha256


def verify_asset_files(root: Path, entries: list[dict], *, digest=file_sha256) -> list[str]:
    """Check local SHA256 or official Git-object hashes without model imports."""
    root = Path(root)
    resolved_root = root.resolve()
    missing = []
    for entry in entries:
        path = root / entry["path"]
        if not path.resolve().is_relative_to(resolved_root):
            raise ValueError(f"unsafe local asset path: {entry['path']}")
        if not path.is_file():
            missing.append(str(path))
            continue
        if "sha256" in entry:
            actual, expected = digest(path), entry["sha256"]
        else:
            content = path.read_bytes()
            actual = hashlib.sha1(
                b"blob " + str(len(content)).encode() + b"\0" + content
            ).hexdigest()
            expected = entry["git_blob_sha1"]
        if actual != expected:
            raise ValueError(f"Asset checksum mismatch (SHA256/official-object hash mismatch): {path}")
    return missing


def resolve_training_registry(
    reference: Path, expected_sha256: str | None = None
) -> tuple[Path, dict]:
    """Resolve only the current definition; reject stale prepared-input bindings."""
    reference = reference.resolve()
    value = json.loads(reference.read_text())
    actual = file_sha256(reference)
    if value.get("compatible_manifests"):
        raise ValueError("Historical training registries are not supported")
    if "manifest" not in value:
        if expected_sha256 is not None and actual != expected_sha256:
            raise ValueError("Protocol registry hash differs")
        return reference, _load_registry_provider(reference, value)
    if expected_sha256 is not None and expected_sha256 not in {actual, value["sha256"]}:
        raise ValueError("Protocol registry hash differs")
    manifest = (reference.parent / value["manifest"]).resolve()
    if file_sha256(manifest) != value["sha256"]:
        raise ValueError("Protocol manifest hash differs")
    return manifest, _load_registry_provider(manifest, json.loads(manifest.read_text()))


def _load_registry_provider(reference: Path, value: dict) -> dict:
    """Load an explicitly injected package provider, without a core plugin dependency."""
    provider = value.get("provider")
    if provider is None:
        return value
    module_name, attribute = provider["entrypoint"].split(":", 1)
    spec = importlib.util.find_spec(module_name)
    if spec is None or spec.origin is None or file_sha256(Path(spec.origin)) != provider["sha256"]:
        raise ValueError("Training registry provider hash differs")
    module = importlib.import_module(module_name)
    loader = getattr(module, attribute)
    return loader(reference, value)


def method_fingerprint(model: str, entries: list[dict]) -> str:
    fields = ("path", "sha256", "bytes", "executable", "external")
    payload = {
        "model": model,
        "files": [
            {key: entry[key] for key in fields}
            for entry in sorted(entries, key=lambda item: item["path"])
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_generated_inputs(
    base: Path, method: dict, *, digest=file_sha256, remove_prefix: str = ""
) -> None:
    """Check data-bound TOML against its validated template and declared file hashes."""
    from .toml import load_toml

    def local(name: str) -> Path:
        path = (base / name.removeprefix(remove_prefix)).resolve()
        if not path.is_relative_to(base.resolve()):
            raise ValueError("Nonlocal generated training input")
        return path

    for item in method.get("generated_inputs", []):
        expected = load_toml(local(item["template"]))
        expected.update({key: digest(local(path)) for key, path in item["hash_fields"].items()})
        if load_toml(local(item["path"])) != expected:
            raise ValueError(f"Generated training configuration differs: {item['path']}")


def protocol_file_source(manifest: Path, entry: dict) -> Path:
    if entry["external"]:
        raise ValueError(f"External protocol asset required: {entry['path']}")
    path = (manifest.parent / entry["source"]).resolve()
    shared = manifest.parent.parent / "shared"
    resource_root = (manifest.parent / entry.get("resource_root", ".")).resolve()
    if (
        not path.is_relative_to(manifest.parent.resolve())
        and not path.is_relative_to(shared.resolve())
        and not path.is_relative_to(resource_root)
    ) or not path.is_file():
        raise ValueError("Nonlocal or missing protocol file")
    if path.stat().st_size != entry["bytes"] or file_sha256(path) != entry["sha256"]:
        raise ValueError(f"Protocol file hash differs: {entry['path']}")
    return path


def training_method(
    manifest: Path, registry: dict, model: str, method_id: str | None = None
) -> dict:
    method_id = method_id or registry["models"][model]["default_method"]
    method = registry["methods"][method_id]
    if method["model"] != model or method["mode"] != "train":
        raise ValueError("Training method model/mode differs")
    entries = [
        registry["files"][entry] if isinstance(entry, str) else entry for entry in method["files"]
    ]
    if method_fingerprint(model, entries) != method["fingerprint"]:
        raise ValueError("Training method fingerprint differs")
    if method_id != f"{model}/train/{method['fingerprint'][:16]}":
        raise ValueError("Training method ID differs")
    paths = set()
    for entry in entries:
        path = PurePosixPath(entry["path"])
        if path.is_absolute() or ".." in path.parts or entry["path"] in paths:
            raise ValueError("Unsafe or duplicate protocol input path")
        paths.add(entry["path"])
        if not entry["external"]:
            protocol_file_source(manifest, entry)
    if "common/__bohr_task_script.sh" not in paths:
        raise ValueError("Training method has no worker entry point")
    return {**method, "files": entries}


def expand_training_config(config: Path, value: dict) -> dict:
    """Keep experiments limited to selection and data bindings; methods own defaults."""
    if "training_protocol" not in value:
        return value
    reference = config.resolve().parent / value["training_protocol"]["reference"]
    manifest, registry = resolve_training_registry(reference)
    forbidden = set(value) - {"training_protocol", "experiment_id", "models", "dataset", "split"}
    if forbidden:
        raise ValueError(f"Training settings must come from the protocol: {sorted(forbidden)}")
    result = json.loads(json.dumps(registry["benchmark_defaults"]))
    result["training_protocol"] = {"manifest": str(manifest), "sha256": file_sha256(manifest)}
    result.update({key: value[key] for key in ("experiment_id", "models") if key in value})
    for section in ("dataset", "split"):
        for key, item in value.get(section, {}).items():
            if key in result[section] and result[section][key] != item:
                raise ValueError(f"Experiment changes locked protocol setting: {section}.{key}")
            result[section][key] = item
    return result
