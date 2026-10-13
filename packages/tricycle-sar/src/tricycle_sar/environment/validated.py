"""Explicit environment execution delegates to the smoke-validated package profile."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tricycle_sar.backends import get_backend
from tricycle_sar.training import digest, protocol_file


def plan_backend(model: str, root: Path) -> dict:
    from tricycle_core.toml import load_toml

    name = model
    environment_model = (
        "chemprop"
        if name.startswith("chemprop_")
        else "classical"
        if name.startswith("fingerprint_")
        else model
    )
    backend = get_backend(name)
    method = backend.method
    recipe = next(
        (item for item in method["files"] if item["path"] == "common/sar_model_envs/recipes.toml"),
        None,
    )
    if recipe:
        catalog = load_toml(protocol_file(name, recipe["path"]))
        spec = catalog["models"][environment_model]
    else:
        catalog = {}
        spec = load_toml(
            protocol_file(name, f"common/{method['bundle_name']}/{method['config_template']}")
        )
    return {
        "model": model,
        "directory": str(root.resolve() / f"{model}-{method['fingerprint'][:12]}"),
        **backend.environment,
        "mode": spec.get("mode", method["environment_mode"]),
        "repository": spec.get("repository", ""),
        "revision": spec.get("revision", ""),
        "python": spec.get("python", catalog.get("base_python")),
        "torch": spec.get("expected_torch", catalog.get("base_torch")),
        "initialization": "validated package worker",
        "training_started": False,
        "download_assets": False,
    }


def initialize_backend(model: str, root: Path, *, assets_directory: Path | None = None) -> dict:
    name = model
    environment_model = (
        "chemprop"
        if name.startswith("chemprop_")
        else "classical"
        if name.startswith("fingerprint_")
        else model
    )
    backend = get_backend(name)
    method = backend.method
    initializer = "common/sar_model_envs/engine.py"
    if initializer not in {item["path"] for item in method["files"]}:
        raise ValueError(
            f"{name} initializes through its validated worker; use get_backend(...).prepare"
        )
    source = (
        protocol_file(name, f"common/{environment_model}.tar.gz")
        if f"common/{environment_model}.tar.gz" in {e["path"] for e in method["files"]}
        else None
    )
    with tempfile.TemporaryDirectory(prefix="tricycle-environment-entry-") as temporary:
        entry = Path(temporary)
        for item in method["files"]:
            if item["path"].startswith(("common/sar_model_envs/", "common/sar_runtime/")):
                target = entry / item["path"].removeprefix("common/")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(protocol_file(name, item["path"]), target)
        command = [
            sys.executable,
            str(entry / "sar_model_envs/engine.py"),
            "--model",
            environment_model,
            "--root",
            str(root),
            "--execute",
        ]
        if source:
            command += ["--source-archive", str(source), "--source-sha256", digest(source)]
        if assets_directory is not None:
            command += ["--assets-directory", str(assets_directory)]
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout[result.stdout.rfind("\n{") + 1 :])
