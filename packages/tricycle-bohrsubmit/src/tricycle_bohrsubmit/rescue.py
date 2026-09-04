from __future__ import annotations

import csv
import shlex
from pathlib import Path

from tricycle_core.errors import InvalidRequestError

from .config import dump_toml, flatten_sections, load_toml
from .models import RescueRequest
from .staging import (
    StagedRun,
    _assert_empty_output,
    _copy_path,
    _prepared_results,
    _write_env,
    _write_tsv,
)
from .utils import compact_name, posix_cksum, worker_label


def _read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise InvalidRequestError(f"TSV file not found: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter="\t")]


def _read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise InvalidRequestError(f"environment file not found: {path}")
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        parsed = shlex.split(raw, comments=False, posix=True)
        result[key.strip()] = parsed[0] if parsed else ""
    return result


def _summary_statuses(download_dir: Path | None, source_worker: Path) -> dict[str, str]:
    summary_paths: list[Path] = []
    source_summary = source_worker / "results" / "g16-summary.tsv"
    if source_summary.is_file():
        summary_paths.append(source_summary)
    if download_dir is not None and download_dir.is_dir():
        summary_paths.extend(sorted(download_dir.rglob("g16-summary.tsv")))
    statuses: dict[str, str] = {}
    for summary in summary_paths:
        try:
            rows = _read_tsv(summary)
        except InvalidRequestError:
            continue
        for row in rows:
            gjf = Path(row.get("gjf", "")).name
            status = row.get("status", "").strip().upper()
            if not gjf or not status:
                continue
            if statuses.get(gjf) != "OK":
                statuses[gjf] = status
    return statuses


def _download_rows(downloads: Path) -> dict[str, Path]:
    result_file = downloads / "download-results.tsv"
    if not result_file.is_file():
        return {}
    result: dict[str, Path] = {}
    for row in _read_tsv(result_file):
        worker = row.get("worker", "")
        directory = row.get("download_dir", "")
        if worker and directory:
            result[worker] = Path(directory)
    return result


def _copy_worker_support(source: Path, target: Path, batch_env: dict[str, str]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / "results").mkdir(exist_ok=True)
    for name in ("work.sh", "run_worker.sh", "dpa-berny.env", "checkpoint"):
        source_file = source / name
        if source_file.is_file():
            _copy_path(source_file, target / name)
            if name in {"work.sh", "run_worker.sh"}:
                (target / name).chmod(0o755)
    for name in (batch_env.get("model_name", ""), batch_env.get("frozen_model", "")):
        if name and (source / name).is_file() and not (target / name).exists():
            _copy_path(source / name, target / name)
    source_common = source / "common"
    if source_common.is_dir():
        target_common = target / "common"
        target_common.mkdir(exist_ok=True)
        for item in source_common.iterdir():
            if not (target_common / item.name).exists():
                _copy_path(item, target_common / item.name)


def prepare_rescue(request: RescueRequest, *, run_id: str, workdir: Path) -> StagedRun:
    source_run = request.source_run.resolve()
    if not source_run.is_dir():
        raise InvalidRequestError(f"source run directory not found: {source_run}")
    source_config = load_toml(source_run / "batch.toml") if (source_run / "batch.toml").is_file() else None
    source_env: dict[str, object] = (
        flatten_sections(source_config)
        if source_config is not None
        else _read_env(source_run / "batch.env")
    )
    source_submit_plan = _read_tsv(source_run / "submit-plan.tsv")
    source_tasks = _read_tsv(source_run / "tasks.tsv")
    if not source_tasks or "gjf" not in source_tasks[0]:
        raise InvalidRequestError("rescue currently requires a DPA-Berny/Gaussian tasks.tsv")

    downloads_root = (request.downloads or source_run / "downloads").resolve()
    download_map: dict[str, Path] = {}
    if (downloads_root / "download-results.tsv").is_file():
        download_map = _download_rows(downloads_root)
    elif not request.allow_missing_downloads:
        raise InvalidRequestError(
            f"download-results.tsv not found: {downloads_root / 'download-results.tsv'}"
        )

    source_workers = {
        row.get("worker", ""): Path(row.get("input_dir", ""))
        for row in source_submit_plan
        if row.get("worker") and row.get("input_dir")
    }
    source_prefix = str(source_env.get("job_prefix", "dpa-berny"))
    job_prefix = compact_name(request.job_prefix or f"{source_prefix}-{run_id}-rescue")
    group_name = compact_name(request.job_group_name or job_prefix)
    output = (request.out_dir or workdir / "runs" / f"{source_run.name}-rescue-{run_id}").resolve()
    _assert_empty_output(output)
    worker_dirs = [
        output / worker_label(index, request.workers) for index in range(request.workers)
    ]
    for worker_dir in worker_dirs:
        (worker_dir / "results").mkdir(parents=True)

    counts = [0] * request.workers
    rescue_rows: list[dict[str, object]] = []
    task_rows: list[dict[str, object]] = []
    support_copied: set[int] = set()
    for task in source_tasks:
        source_worker_name = task.get("worker", "")
        source_worker = source_workers.get(source_worker_name)
        gjf = Path(task.get("gjf", "")).name
        if source_worker is None or not gjf:
            raise InvalidRequestError(f"invalid source task row: {task}")
        target_index = posix_cksum(f"{source_worker_name}/{gjf}") % request.workers
        target_worker_name = worker_label(target_index, request.workers)
        status = _summary_statuses(download_map.get(source_worker_name), source_worker).get(gjf, "")
        if status == "OK":
            action = "SKIP_COMPLETED"
            rescue_rows.append(
                {
                    "source_worker": source_worker_name,
                    "source_xyz": task.get("source_xyz", ""),
                    "gjf": gjf,
                    "status": status,
                    "action": action,
                    "target_worker": "",
                }
            )
            continue
        if status == "FAILED" and not request.retry_failed_tasks:
            action = "SKIP_FAILED"
            rescue_rows.append(
                {
                    "source_worker": source_worker_name,
                    "source_xyz": task.get("source_xyz", ""),
                    "gjf": gjf,
                    "status": status,
                    "action": action,
                    "target_worker": "",
                }
            )
            continue

        target_dir = worker_dirs[target_index]
        if target_index not in support_copied:
            _copy_worker_support(source_worker, target_dir, source_env)
            support_copied.add(target_index)
        source_gjf = source_worker / gjf
        if not source_gjf.is_file():
            raise InvalidRequestError(f"source task file not found: {source_gjf}")
        _copy_path(source_gjf, target_dir / gjf)
        json_names = [item for item in task.get("json", "").split(",") if item]
        for json_name in json_names:
            source_json = source_worker / Path(json_name).name
            if not source_json.is_file():
                raise InvalidRequestError(f"source JSON file not found: {source_json}")
            _copy_path(source_json, target_dir / source_json.name)
        with (target_dir / "manifest.txt").open("a", encoding="utf-8") as handle:
            handle.write(f"{gjf}\n")
        with (target_dir / "manifest.tsv").open("a", encoding="utf-8") as handle:
            handle.write(
                f"{task.get('source_xyz', '')}\t{gjf}\t{task.get('json', '')}\t{task.get('traj', '')}\n"
            )
        task_rows.append(
            {
                "worker": target_worker_name,
                "source_xyz": task.get("source_xyz", ""),
                "gjf": gjf,
                "json": task.get("json", ""),
                "traj": task.get("traj", ""),
            }
        )
        counts[target_index] += 1
        rescue_rows.append(
            {
                "source_worker": source_worker_name,
                "source_xyz": task.get("source_xyz", ""),
                "gjf": gjf,
                "status": status or "MISSING",
                "action": "REQUEUE",
                "target_worker": target_worker_name,
            }
        )

    new_env = {
        key: value
        for key, value in source_env.items()
        if not isinstance(value, (dict, list, tuple))
    }
    new_env.update(
        {
            "run_id": run_id,
            "workers": request.workers,
            "job_prefix": job_prefix,
            "job_group_name": group_name,
            "job_group_id": request.job_group_id or "",
            "auto_job_group": int(request.auto_job_group),
        }
    )
    _write_env(output / "batch.env", new_env)
    if source_config is None:
        source_dataset_paths = []
        dataset_file = source_run / "dataset-paths.txt"
        if dataset_file.is_file():
            source_dataset_paths = [
                line for line in dataset_file.read_text(encoding="utf-8").splitlines() if line
            ]
        source_extra_args = []
        extra_file = source_run / "submit-extra-args.txt"
        if extra_file.is_file():
            source_extra_args = [
                line for line in extra_file.read_text(encoding="utf-8").splitlines() if line
            ]
        new_config: dict[str, object] = {
            "run": {},
            "bohr": {
                "image": source_env.get("image", ""),
                "project_id": source_env.get("project_id", ""),
                "machine_type": source_env.get("machine_type", ""),
                "backward_files": source_env.get("backward_files", ""),
                "log_file": source_env.get("log_file", "worker.log"),
                "result_path": source_env.get("result_path", ""),
                "max_reschedule_times": source_env.get("max_reschedule_times", ""),
                "max_run_time": source_env.get("max_run_time", ""),
                "nnode": source_env.get("nnode", ""),
                "dataset_paths": source_dataset_paths,
                "extra_submit_args": source_extra_args,
            },
            "workflow": {"kind": "dpa-berny-rescue"},
        }
    else:
        new_config = dict(source_config)
    run_config = new_config.get("run")
    run_section = dict(run_config) if isinstance(run_config, dict) else {}
    run_section.update(
        {
            "run_id": run_id,
            "workers": request.workers,
            "job_prefix": job_prefix,
            "job_group_name": group_name,
            "job_group_id": request.job_group_id or "",
            "auto_job_group": request.auto_job_group,
        }
    )
    new_config["run"] = run_section
    dump_toml(output / "batch.toml", new_config)
    _write_tsv(output / "tasks.tsv", ("worker", "source_xyz", "gjf", "json", "traj"), task_rows)
    submit_rows = [
        {
            "worker": worker_label(index, request.workers),
            "tasks": counts[index],
            "job_name": f"{job_prefix}-{worker_label(index, request.workers)}",
            "input_dir": str(worker_dirs[index]),
        }
        for index in range(request.workers)
    ]
    _write_tsv(output / "submit-plan.tsv", ("worker", "tasks", "job_name", "input_dir"), submit_rows)
    _prepared_results(output, submit_rows, "PREPARED")
    _write_tsv(
        output / "rescue-summary.tsv",
        ("source_worker", "source_xyz", "gjf", "status", "action", "target_worker"),
        rescue_rows,
    )
    return StagedRun(run_id, output, request.workers, len(task_rows), tuple(counts), "rescue")
