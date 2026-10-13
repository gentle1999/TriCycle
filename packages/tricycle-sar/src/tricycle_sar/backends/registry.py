"""Package-owned SAR environments and runners, checked against smoke-validated bytes."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from tricycle_core.toml import load_toml
from tricycle_core.training_protocol import file_sha256, method_fingerprint

CATALOG = Path(__file__).with_name("catalog.json")


def load_registry(reference: Path, definition: dict) -> dict:
    """Expand reusable environment/run definitions; never write a second manifest."""
    index = reference.parent / definition["integrity"]["path"]
    if file_sha256(index) != definition["integrity"]["sha256"]:
        raise ValueError("SAR runtime integrity index differs")
    roots = [reference.parent.parent.resolve()]
    shared_roots = {}
    for module, relative in definition.get("shared_source_roots", {}).items():
        spec = importlib.util.find_spec(module)
        if spec is None or spec.origin is None:
            raise ValueError(f"Invalid shared SAR source root: {module}")
        root = Path(spec.origin).parent.resolve()
        shared_roots[relative.rstrip("/") + "/"] = root
        roots.append(root)

    def source_path(name: str) -> Path:
        # Catalog names are logical references, independent of the workspace layout.
        for prefix, root in shared_roots.items():
            if name.startswith(prefix):
                path = (root / name.removeprefix(prefix)).resolve()
                if not path.is_relative_to(root):
                    raise ValueError(f"Unsafe shared SAR source: {name}")
                return path
        return (reference.parent / name).resolve()

    hashes = {}
    for line in index.read_text().splitlines():
        expected, name = line.split("  ", 1)
        path = source_path(name)
        if (
            not any(path.is_relative_to(root) for root in roots)
            or name in hashes
            or not path.is_file()
            or file_sha256(path) != expected
        ):
            raise ValueError(f"Smoke-validated SAR runtime differs: {name}")
        hashes[name] = expected

    def entry(target: str, source: str | dict) -> dict:
        if isinstance(source, dict):
            return {"path": target, "external": True, "executable": False, **source}
        path = source_path(source)
        if source not in hashes:
            raise ValueError(f"Unverified SAR runtime source: {source}")
        return {
            "path": target,
            "source": os.path.relpath(path, reference.parent),
            "resource_root": next(
                os.path.relpath(root, reference.parent)
                for root in roots
                if path.is_relative_to(root)
            ),
            "sha256": hashes[source],
            "bytes": path.stat().st_size,
            "executable": path.suffix == ".sh",
            "external": False,
        }

    models: dict[str, dict] = {}
    methods: dict[str, dict] = {}
    for model, selection in definition["models"].items():
        environment = definition["environments"][selection["environment"]]
        run = definition["runs"][selection["run"]]
        files = [entry(path, source) for path, source in environment["files"].items()]
        files += [
            entry("common/__bohr_task_script.sh", "workers/launch.sh"),
            entry("common/sar_model_worker.sh", run["worker"]),
            entry("common/sar_outputs.py", "outputs.py"),
        ]
        files += [
            entry(f"common/{run['bundle_name']}/{path}", source)
            for path, source in run["files"].items()
        ]
        fingerprint = method_fingerprint(model, files)
        method_id = f"{model}/train/{fingerprint[:16]}"
        method = {
            **{key: value for key, value in environment.items() if key != "files"},
            "model": model,
            "mode": "train",
            "fingerprint": fingerprint,
            "bundle_name": run["bundle_name"],
            "entrypoint": "common/__bohr_task_script.sh",
            "environment": selection["environment"],
            "run": selection["run"],
            "files": files,
        }
        if "config_template" in run:
            method["config_template"] = run["config_template"]
            method["config_name"] = run["config_name"]
            prefix = f"common/{run['bundle_name']}/"
            method["generated_inputs"] = [
                {
                    "template": prefix + run["config_template"],
                    "path": prefix + run["config_name"],
                    "hash_fields": {
                        "dataset_sha256": prefix + "data/snapshots/ground-truth/dataset.csv",
                        "splits_sha256": prefix
                        + "data/splits/ground-truth-5fold/assignments.csv",
                    },
                }
            ]
        methods[method_id] = method
        models[model] = {"default_method": method_id}
    defaults = json.loads(json.dumps(definition["experiment_defaults"]))
    for model, key in (("fingerprint_rf", "classical"), ("uam", "uam"), ("molclr", "molclr")):
        run = definition["runs"][definition["models"][model]["run"]]
        config = load_toml(source_path(run["files"][run["config_template"]]))
        defaults[key] = config
    run = definition["runs"][definition["models"]["reactiont5"]["run"]]
    defaults["reactiont5"] = json.loads(source_path(run["files"]["reactiont5.json"]).read_text())
    return {
        "schema": definition["schema"],
        "models": models,
        "methods": methods,
        "benchmark_defaults": defaults,
    }


def render_config(bundle: Path, method: dict) -> None:
    """Bind data to a validated parameter template, without changing model settings."""
    template = method.get("config_template")
    if template is None:
        return
    from tricycle_core.toml import dump_toml

    config = load_toml(bundle / template)
    dataset, splits = bundle / config["dataset"], bundle / config["splits"]
    if not dataset.is_file() or not splits.is_file():
        raise ValueError("Model configuration needs the experiment dataset and splits")
    config["dataset_sha256"] = file_sha256(dataset)
    config["splits_sha256"] = file_sha256(splits)
    dump_toml(bundle / method["config_name"], config)
