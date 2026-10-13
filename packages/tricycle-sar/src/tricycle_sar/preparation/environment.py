"""Prepare isolated environment smoke inputs from the registered SAR framework."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from tricycle_core.io import file_sha256
from tricycle_core.toml import dump_toml, load_toml
from tricycle_core.training_protocol import protocol_file_source, verify_asset_files

from tricycle_sar import get_backend, list_backends
from tricycle_sar.backends.registry import CATALOG


def prepare(model: str, output: Path, *, assets: Path | None = None, group_config: Path) -> Path:
    name = "chemprop_cgr" if model == "chemprop" else model
    backend = get_backend(name)
    method = backend.method
    if method["executor"] != "bohr":
        raise ValueError(f"{model} requires its pinned internal executor")
    environment_model = (
        "classical"
        if name.startswith("fingerprint_")
        else "chemprop"
        if name.startswith("chemprop_")
        else name
    )
    entries = {e["path"]: e for e in method["files"]}
    recipe = protocol_file_source(CATALOG, entries["common/sar_model_envs/recipes.toml"])
    spec = load_toml(recipe)["models"][environment_model]
    required = spec.get("assets", []) + spec.get("local_wheels", [])
    if required and (assets is None or verify_asset_files(assets, required)):
        raise ValueError("Complete local fixed environment assets are required")
    if output.exists():
        raise FileExistsError(output)
    common = output / "common"
    for entry in method["files"]:
        if entry["path"] == "common/__bohr_task_script.sh":
            continue
        if entry["path"].startswith(f"common/{method['bundle_name']}/"):
            continue
        target = output / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(protocol_file_source(CATALOG, entry), target)
    bundle = common / "environment_bundle"
    bundle.mkdir()
    for entry in required:
        target = bundle / "assets" / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(assets / entry["path"], target)
    worker = common / "__bohr_task_script.sh"
    worker.write_text("""#!/usr/bin/env bash
set -euo pipefail
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy PYTHONHOME
export NO_PROXY='*' no_proxy='*' PYTHONNOUSERSITE=1 PYTHONPATH="$PWD/common"
exec python3 "$PWD/common/sar_execution.py" --task "$1" --common "$PWD/common" --result "$PWD/results" --initialize-only
""")
    worker.chmod(0o755)
    task = {
        "model": name,
        "environment_model": environment_model,
        "fold": 0,
        "smoke": True,
        "bundle_name": "environment_bundle",
        "input_hashes": {
            p.relative_to(common).as_posix(): file_sha256(p)
            for p in common.rglob("*")
            if p.is_file()
        },
    }
    archive = common / f"{environment_model}.tar.gz"
    if archive.is_file():
        task["source_sha256"] = file_sha256(archive)
    dump_toml(output / "task.toml", task)
    group = load_toml(group_config)
    request = {
        **{k: v for k, v in group.items() if k in ("project_id", "job_group_id")},
        "task_patterns": [str((output / "task.toml").resolve())],
        "workers": 1,
        "script": str(worker.resolve()),
        "common_paths": [str(p.resolve()) for p in common.iterdir() if p != worker],
        "machine_type": method["machine_type"],
        "image": method["image"],
        "job_prefix": f"sar-environment-{name}",
        "auto_job_group": False,
        "max_run_time": "60",
        "max_reschedule_times": "0",
        "backward_files": "worker.log,results",
    }
    dump_toml(output / "bohr-smoke.toml", request)
    return output / "bohr-smoke.toml"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=[*list_backends(), "chemprop"], required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--group-config", required=True, type=Path, help="TOML with explicit project_id/job_group_id")
    args = parser.parse_args()
    print(prepare(args.model, args.out_dir, assets=args.assets, group_config=args.group_config))


if __name__ == "__main__":
    main()
