"""Build local SAR requests from one pinned method and separate scheduling choices."""

from __future__ import annotations

from pathlib import Path

from tricycle_bohrsubmit.task_table import expand_task_specs
from tricycle_core.toml import dump_toml, load_toml

from tricycle_sar.records import compact_tasks
from tricycle_sar.training import apply_protocol, digest, protocol


def bundle_hashes(bundle: Path) -> list[dict[str, str]]:
    return [
        {"path": path.relative_to(bundle).as_posix(), "sha256": digest(path)}
        for path in sorted(bundle.rglob("*"))
        if path.is_file()
    ]


def write_bohr_request(
    model: str,
    directory: Path,
    bundle: Path,
    *,
    task_defaults: dict,
    task_specs: list[dict],
    workers: int,
    job_prefix: str,
    max_run_time: str,
    filename: str,
    group_name: str | None = None,
    project_id: str | None = None,
    group_config: Path | None = None,
) -> Path:
    if project_id:
        group = {"project_id": project_id, "job_group_id": ""}
    elif group_config is not None:
        group = load_toml(group_config)
    else:
        raise ValueError("SAR submission requires explicit group_config or project_id")
    expanded = expand_task_specs(task_defaults, task_specs)
    task_defaults, task_specs = compact_tasks(
        [{"name": name, **payload} for name, payload in expanded]
    )
    record, _ = protocol(model)
    if record["executor"] != "bohr":
        raise ValueError(f"{model} requires its pinned internal executor")
    task_defaults["bundle_name"] = record["bundle_name"]
    task_defaults.setdefault("environment_model", "classical" if record["bundle_name"] == "classical_bundle" else "chemprop" if model.startswith("chemprop_") else model)
    request = {
        "task_defaults": task_defaults,
        "task_specs": task_specs,
        "workers": workers,
        "common_paths": [str(bundle.resolve())],
        "project_id": project_id or group["project_id"],
        "job_group_id": "" if group_name else group["job_group_id"],
        "auto_job_group": group_name is not None,
        "machine_type": record["machine_type"],
        "image": record["image"],
        "job_prefix": job_prefix,
        "max_run_time": max_run_time,
        "max_reschedule_times": "0",
        "extra_submit_args": ["--yes"],
        "backward_files": "worker.log,results",
    }
    if group_name is not None:
        request["job_group_name"] = group_name
    apply_protocol(model, request, directory, bundle)
    path = directory / filename
    dump_toml(path, request)
    return path
