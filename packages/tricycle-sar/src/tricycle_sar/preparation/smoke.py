"""Prepare model forward/backward probes with registered environment profiles."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from tricycle_core.io import file_sha256, write_json
from tricycle_core.toml import dump_toml, load_toml
from tricycle_core.training_protocol import protocol_file_source

from tricycle_sar import get_backend
from tricycle_sar.backends.registry import CATALOG


def prepare(
    model: str,
    benchmark: Path,
    output: Path,
    *,
    assets: Path | None = None,
    group_config: Path,
) -> Path:
    backend = get_backend(model)
    if model not in ("rxnresid", "reactiont5", "rxngraphormer"):
        raise ValueError("Unsupported model probe")
    if backend.environment["executor"] != "bohr":
        raise ValueError(f"{model} requires its pinned internal executor")
    files = {entry["path"]: entry for entry in backend.method["files"]}
    original = protocol_file_source(CATALOG, files[f"common/{model}.tar.gz"])
    required = []
    prefix = f"common/{backend.method['bundle_name']}/assets/"
    for entry in backend.method["files"]:
        if entry["external"]:
            candidate = (
                assets / entry["path"].removeprefix(prefix)
                if assets
                else None
            )
            if (
                candidate is None
                or not candidate.is_file()
                or file_sha256(candidate) != entry["sha256"]
            ):
                raise ValueError(f"Required fixed probe asset missing or changed: {entry['path']}")
            required.append((entry, candidate))
    if output.exists():
        raise FileExistsError(output)
    common = output / "common"
    bundle = common / "probe_bundle"
    bundle.mkdir(parents=True)
    for entry in backend.method["files"]:
        # Probes run the shared worker directly; the prediction collector is
        # for training jobs and would collide with staging's task-script name.
        if entry["path"] == "common/__bohr_task_script.sh":
            continue
        if entry["external"]:
            candidate = next(candidate for selected, candidate in required if selected is entry)
        else:
            candidate = protocol_file_source(CATALOG, entry)
        relative = entry["path"].removeprefix("common/")
        if relative == "sar_model_worker.sh":
            relative = "__bohr_task_script.sh"
        if relative.startswith(backend.method["bundle_name"] + "/"):
            relative = "probe_bundle/" + relative.split("/", 1)[1]
        target = common / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, target)
    shutil.copytree(benchmark / "workspace/data", bundle / "data", dirs_exist_ok=True)
    from tricycle_sar.benchmark import sha256

    entries = [
        {"path": str(p.relative_to(bundle)), "sha256": sha256(p)}
        for p in sorted(bundle.rglob("*"))
        if p.is_file() and p.name != "probe.json"
    ]
    write_json(bundle / "probe.json", {"files": entries, "model": model, "downloads": []})
    task = {
        "model": model,
        "fold": 0,
        "smoke": True,
        "bundle_name": "probe_bundle",
        "environment_model": model,
        "source_sha256": file_sha256(original),
    }
    task["input_hashes"] = {
        p.relative_to(common).as_posix(): file_sha256(p)
        for p in common.rglob("*")
        if p.is_file()
    }
    dump_toml(output / "task.toml", task)
    group = load_toml(group_config)
    request = {
        **{k: v for k, v in group.items() if k in ("project_id", "job_group_id")},
        "task_patterns": [str((output / "task.toml").resolve())],
        "workers": 1,
        "script": str((common / "__bohr_task_script.sh").resolve()),
        "common_paths": [
            str(p.resolve()) for p in common.iterdir() if p.name != "__bohr_task_script.sh"
        ],
        "machine_type": backend.environment["machine_type"],
        "image": backend.environment["image"],
        "job_prefix": f"sar-{model}-smoke",
        "auto_job_group": False,
        "max_run_time": "60",
        "max_reschedule_times": "0",
        "backward_files": "worker.log,results",
    }
    dump_toml(output / "bohr-smoke.toml", request)
    return output / "bohr-smoke.toml"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", choices=["rxnresid", "reactiont5", "rxngraphormer"], required=True
    )
    parser.add_argument("--benchmark-run", type=Path, required=True)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--group-config", required=True, type=Path, help="TOML with explicit project_id/job_group_id")
    args = parser.parse_args()
    print(prepare(args.model, args.benchmark_run, args.out_dir, assets=args.assets, group_config=args.group_config))


if __name__ == "__main__":
    main()
