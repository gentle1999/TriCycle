from __future__ import annotations

import csv
import glob
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tricycle_core.errors import InvalidRequestError

from .config import dump_toml
from .dpa import build_dpa_tasks, render_task_files
from .models import BatchRequest, DpaBernyRequest
from .utils import compact_name, posix_cksum, short_hash, worker_label


@dataclass(frozen=True, slots=True)
class StagedRun:
    run_id: str
    run_dir: Path
    workers: int
    task_count: int
    worker_task_counts: tuple[int, ...]
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "workers": self.workers,
            "task_count": self.task_count,
            "worker_task_counts": list(self.worker_task_counts),
            "kind": self.kind,
        }


def _expand_patterns(patterns: tuple[str, ...]) -> tuple[Path, ...]:
    matches: set[Path] = set()
    for pattern in patterns:
        path = Path(pattern)
        if path.is_file() or path.is_dir():
            matches.add(path.resolve())
            continue
        for item_name in glob.glob(pattern, recursive=True):
            item = Path(item_name)
            if item.is_file() or item.is_dir():
                matches.add(item.resolve())
    return tuple(sorted(matches, key=str))


def _assert_empty_output(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise InvalidRequestError(f"output directory is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def _copy_path(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _write_env(path: Path, values: dict[str, object]) -> None:
    """Write a shell environment bridge for worker scripts and compatibility."""

    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Runtime environment bridge; declarative configuration is in batch.toml.\n")
        for key, value in values.items():
            handle.write(f"{key}={shlex.quote(str(value))}\n")


def _write_tsv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in fieldnames} for row in rows)


def _generic_worker_runner() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

task_script='./common/__bohr_task_script.sh'
manifest='./manifest.txt'
[[ -f \"$task_script\" ]] || { printf '[worker] missing task script: %s\\n' \"$task_script\" >&2; exit 2; }
[[ -f \"$manifest\" ]] || { printf '[worker] missing manifest: %s\\n' \"$manifest\" >&2; exit 2; }
mkdir -p results
count=0
while IFS= read -r task_path || [[ -n \"$task_path\" ]]; do
  [[ -n \"$task_path\" ]] || continue
  count=$((count + 1))
  printf '[worker] start task %d: %s\\n' \"$count\" \"$task_path\"
  bash \"$task_script\" \"$task_path\"
  printf '[worker] finish task %d: %s\\n' \"$count\" \"$task_path\"
done <\"$manifest\"
printf '[worker] completed %d task(s)\\n' \"$count\"
"""


def _chemistry_worker(engine: str) -> str:
    if engine == "dpa":
        return """#!/usr/bin/env bash
set -euo pipefail

source ./dpa-berny.env
GAUSSIAN_CMD="${GAUSSIAN_CMD:-g16}"
server_pid=''
cleanup() {
  if [[ -n \"${server_pid:-}\" ]] && kill -0 \"$server_pid\" >/dev/null 2>&1; then
    kill \"$server_pid\" >/dev/null 2>&1 || true
    wait \"$server_pid\" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [[ "${DPA_NO_FREEZE:-0}" != 1 ]]; then
  [[ -f "${DPA_MODEL_INPUT:-model.ckpt.pt}" ]] || { printf '[dpa-berny] missing model checkpoint\\n' >&2; exit 1; }
  dp --pt freeze
fi
server_model="${DPA_SERVER_MODEL:-${DPA_FROZEN_MODEL:-frozen_model.pth}}"
server_script="${DPA_SERVER_SCRIPT:?DPA_SERVER_SCRIPT is required}"
[[ -f "$server_model" ]] || { printf '[dpa-berny] missing server model: %s\\n' "$server_model" >&2; exit 1; }
[[ -f "$server_script" ]] || { printf '[dpa-berny] missing server script: %s\\n' "$server_script" >&2; exit 1; }
python "$server_script" "$server_model" >server.log 2>&1 &
server_pid=$!
sleep "${DPA_SERVER_WAIT:-20}"
kill -0 "$server_pid" >/dev/null 2>&1 || { tail -50 server.log >&2 || true; exit 1; }
""" + _serial_gaussian_tail("dpa")
    if engine == "gaussian":
        return """#!/usr/bin/env bash
set -euo pipefail
source ./dpa-berny.env
GAUSSIAN_CMD="${GAUSSIAN_CMD:-g16}"
""" + _serial_gaussian_tail("gaussian")
    raise InvalidRequestError(f"unsupported chemistry engine: {engine}")


def _serial_gaussian_tail(label: str) -> str:
    return f"""mkdir -p results
printf 'gjf\\tstatus\\texit_code\\n' >results/g16-summary.tsv
failures=0
for file in ./*.gjf; do
  [[ -f \"$file\" ]] || continue
  rc=0
  \"$GAUSSIAN_CMD\" \"$file\" || rc=$?
  if (( rc == 0 )); then
    printf '%s\\tOK\\t0\\n' \"$file\" >>results/g16-summary.tsv
  else
    printf '%s\\tFAILED\\t%s\\n' \"$file\" \"$rc\" >>results/g16-summary.tsv
    failures=$((failures + 1))
  fi
done
if (( failures > 0 )) && [[ "${{DPA_ALLOW_TASK_FAILURES:-${{GAUSSIAN_ALLOW_TASK_FAILURES:-0}}}}" != 1 ]]; then
  printf '[{label}] %d task(s) failed\\n' \"$failures\" >&2
  exit 1
fi
"""


def _stage_common_worker_dirs(run_dir: Path, workers: int) -> list[Path]:
    paths: list[Path] = []
    for index in range(workers):
        worker_dir = run_dir / worker_label(index, workers)
        worker_dir.mkdir(parents=True, exist_ok=True)
        (worker_dir / "results").mkdir()
        paths.append(worker_dir)
    return paths


def _prepared_results(run_dir: Path, submit_rows: list[dict[str, object]], status: str) -> None:
    rows = []
    for row in submit_rows:
        count = int(row["tasks"])
        rows.append(
            {
                "worker": row["worker"],
                "tasks": count,
                "job_id": "",
                "job_name": row["job_name"],
                "status": "SKIPPED_EMPTY" if count == 0 else status,
                "input_dir": row["input_dir"],
            }
        )
    _write_tsv(
        run_dir / "submit-results.tsv",
        ("worker", "tasks", "job_id", "job_name", "status", "input_dir"),
        rows,
    )


def _base_job_metadata(
    *,
    run_id: str,
    workers: int,
    job_prefix: str,
    job_group_name: str,
    job_group_id: str | None,
    auto_job_group: bool,
    image: str,
    project_id: str,
    machine_type: str,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "workers": workers,
        "job_prefix": job_prefix,
        "job_group_name": job_group_name,
        "job_group_id": job_group_id or "",
        "auto_job_group": int(auto_job_group),
        "image": image,
        "project_id": project_id,
        "machine_type": machine_type,
    }


def _base_run_config(
    *,
    run_id: str,
    workers: int,
    job_prefix: str,
    job_group_name: str,
    job_group_id: str | None,
    auto_job_group: bool,
    image: str,
    project_id: str,
    machine_type: str,
) -> dict[str, dict[str, object]]:
    """Return the declarative TOML sections shared by all staged runs."""

    return {
        "run": {
            "run_id": run_id,
            "workers": workers,
            "job_prefix": job_prefix,
            "job_group_name": job_group_name,
            "job_group_id": job_group_id or "",
            "auto_job_group": auto_job_group,
        },
        "bohr": {
            "image": image,
            "project_id": project_id,
            "machine_type": machine_type,
        },
    }


def stage_batch(request: BatchRequest, *, run_id: str, workdir: Path, dry_run: bool) -> StagedRun:
    # Local staging is explicit; dry-run only suppresses remote effects.
    tasks = _expand_patterns(request.task_patterns)
    if not tasks:
        raise InvalidRequestError(f"no task files matched: {request.task_patterns}")
    output = (request.out_dir or workdir / "runs" / run_id).resolve()
    _assert_empty_output(output)

    job_prefix = compact_name(request.job_prefix or f"bohr-batch-{run_id}")
    job_group_name = compact_name(request.job_group_name or job_prefix)
    worker_dirs = _stage_common_worker_dirs(output, request.workers)
    counts = [0] * request.workers
    for worker_dir in worker_dirs:
        (worker_dir / "tasks").mkdir()
        (worker_dir / "common").mkdir()
        if request.worker_script is not None:
            source = request.worker_script.resolve()
            if not source.is_file():
                raise InvalidRequestError(f"worker_script is not a file: {source}")
            _copy_path(source, worker_dir / "run_worker.sh")
        else:
            source = request.script.resolve() if request.script is not None else None
            if source is None or not source.is_file():
                raise InvalidRequestError(f"script is not a file: {source}")
            _copy_path(source, worker_dir / "common" / "__bohr_task_script.sh")
            (worker_dir / "run_worker.sh").write_text(_generic_worker_runner(), encoding="utf-8")
        (worker_dir / "run_worker.sh").chmod(0o755)

    for task_path in tasks:
        worker = posix_cksum(str(task_path)) % request.workers
        label = worker_label(worker, request.workers)
        cloud_path = f"tasks/{short_hash(str(task_path))}_{task_path.name}"
        _copy_path(task_path, output / label / cloud_path)
        with (output / label / "manifest.txt").open("a", encoding="utf-8") as manifest:
            manifest.write(f"{cloud_path}\n")
        with (output / label / "manifest.tsv").open("a", encoding="utf-8") as manifest:
            manifest.write(f"{task_path}\t{cloud_path}\n")
        counts[worker] += 1

    for worker_dir in worker_dirs:
        # The common directory is populated after task staging so duplicate
        # names can be checked consistently for every worker.
        for common in request.common_paths:
            source = common.resolve()
            if not source.exists():
                raise InvalidRequestError(f"common path does not exist: {source}")
            destination = worker_dir / "common" / source.name
            if destination.exists():
                raise InvalidRequestError(f"duplicate staged common basename: {source.name}")
            _copy_path(source, destination)

    batch_env = _base_job_metadata(
        run_id=run_id,
        workers=request.workers,
        job_prefix=job_prefix,
        job_group_name=job_group_name,
        job_group_id=request.job_group_id,
        auto_job_group=request.auto_job_group,
        image=request.image,
        project_id=str(request.project_id),
        machine_type=str(request.machine_type),
    )
    batch_env.update(
        {
            "backward_files": request.backward_files,
            "log_file": request.log_file,
            "result_path": request.result_path or "",
            "max_reschedule_times": request.max_reschedule_times or "",
            "max_run_time": request.max_run_time or "",
            "nnode": request.nnode or "",
        }
    )
    _write_env(output / "batch.env", batch_env)
    batch_config = _base_run_config(
        run_id=run_id,
        workers=request.workers,
        job_prefix=job_prefix,
        job_group_name=job_group_name,
        job_group_id=request.job_group_id,
        auto_job_group=request.auto_job_group,
        image=request.image,
        project_id=str(request.project_id),
        machine_type=str(request.machine_type),
    )
    batch_config["bohr"].update(
        {
            "backward_files": request.backward_files,
            "log_file": request.log_file,
            "result_path": request.result_path or "",
            "max_reschedule_times": request.max_reschedule_times or "",
            "max_run_time": request.max_run_time or "",
            "nnode": request.nnode or "",
            "dataset_paths": list(request.dataset_paths),
            "extra_submit_args": list(request.extra_submit_args),
        }
    )
    dump_toml(output / "batch.toml", batch_config)
    task_rows: list[dict[str, object]] = []
    for worker_dir in worker_dirs:
        manifest = worker_dir / "manifest.tsv"
        if manifest.is_file():
            with manifest.open(encoding="utf-8") as handle:
                for line in handle:
                    source, cloud = line.rstrip("\n").split("\t", 1)
                    task_rows.append(
                        {"worker": worker_dir.name, "source_path": source, "cloud_path": cloud}
                    )
    _write_tsv(output / "tasks.tsv", ("worker", "source_path", "cloud_path"), task_rows)

    submit_rows = [
        {
            "worker": worker_label(index, request.workers),
            "tasks": counts[index],
            "job_name": f"{job_prefix}-{worker_label(index, request.workers)}",
            "input_dir": str(output / worker_label(index, request.workers)),
        }
        for index in range(request.workers)
    ]
    _write_tsv(output / "submit-plan.tsv", ("worker", "tasks", "job_name", "input_dir"), submit_rows)
    _prepared_results(output, submit_rows, "DRY_RUN" if dry_run else "PREPARED")
    return StagedRun(run_id, output, request.workers, len(tasks), tuple(counts), "batch")


def _dpa_env(request: DpaBernyRequest) -> dict[str, object]:
    if request.engine == "dpa":
        return {
            "DPA_MODEL_INPUT": request.model_name,
            "DPA_FROZEN_MODEL": request.frozen_model,
            "DPA_NO_FREEZE": int(request.no_freeze),
            "DPA_SERVER_MODEL": request.model_name if request.no_freeze else request.frozen_model,
            "DPA_ALLOW_TASK_FAILURES": int(request.allow_task_failures),
            "DPA_SERVER_SCRIPT": request.server_script,
            "DPA_SERVER_WAIT": request.server_wait,
            "DPA_SERVER_HEALTH_TIMEOUT": request.server_health_timeout,
            "GAUSSIAN_CMD": "g16",
        }
    return {"GAUSSIAN_CMD": "g16", "GAUSSIAN_ALLOW_TASK_FAILURES": int(request.allow_task_failures)}


def stage_dpa_berny(
    request: DpaBernyRequest,
    *,
    run_id: str,
    workdir: Path,
    dry_run: bool,
) -> StagedRun:
    tasks = build_dpa_tasks(request)
    if request.engine == "dpa":
        assert request.model_path is not None
        model_path = request.model_path.resolve()
        if not model_path.is_file():
            raise InvalidRequestError(f"model_path is not a file: {model_path}")
    else:
        model_path = None

    output = (request.out_dir or workdir / "runs" / run_id).resolve()
    _assert_empty_output(output)
    job_prefix = compact_name(request.job_prefix or f"dpa-berny-{run_id}")
    job_group_name = compact_name(request.job_group_name or job_prefix)
    worker_dirs = _stage_common_worker_dirs(output, request.workers)
    counts = [0] * request.workers
    for worker_dir in worker_dirs:
        (worker_dir / "work.sh").write_text(_chemistry_worker(request.engine), encoding="utf-8")
        (worker_dir / "work.sh").chmod(0o755)
        _write_env(worker_dir / "dpa-berny.env", _dpa_env(request))
        if model_path is not None:
            _copy_path(model_path, worker_dir / request.model_name)
            (worker_dir / "checkpoint").write_text(f"./{request.model_name}", encoding="utf-8")

    batch_env = {
        **_base_job_metadata(
            run_id=run_id,
            workers=request.workers,
            job_prefix=job_prefix,
            job_group_name=job_group_name,
            job_group_id=request.job_group_id,
            auto_job_group=request.auto_job_group,
            image=request.image,
            project_id=str(request.project_id),
            machine_type=str(request.machine_type),
        ),
        "task_type": request.task_type,
        "engine": request.engine,
        "fixed_only": int(request.fixed_only),
        "multi_stage_chk": int(request.multi_stage_chk),
        "ignore_constraints": int(request.ignore_constraints),
        "one_task_per_worker": int(request.one_task_per_worker),
        "charge": request.charge,
        "multiplicity": request.multiplicity,
        "nproc": request.nproc,
        "mem": request.mem,
        "route": request.route or "",
        "opt_route": request.opt_route or "",
        "fixed_route": request.fixed_route or "",
        "model_name": request.model_name,
        "frozen_model": request.frozen_model,
        "no_freeze": int(request.no_freeze),
        "allow_task_failures": int(request.allow_task_failures),
        "functional": request.functional or "",
        "basis": request.basis or "",
        "backward_files": request.backward_files,
        "log_file": request.log_file,
        "result_path": request.result_path or "",
        "max_reschedule_times": request.max_reschedule_times or "",
        "max_run_time": request.max_run_time or "",
        "nnode": request.nnode or "",
    }
    _write_env(output / "batch.env", batch_env)
    batch_config = _base_run_config(
        run_id=run_id,
        workers=request.workers,
        job_prefix=job_prefix,
        job_group_name=job_group_name,
        job_group_id=request.job_group_id,
        auto_job_group=request.auto_job_group,
        image=request.image,
        project_id=str(request.project_id),
        machine_type=str(request.machine_type),
    )
    batch_config["bohr"].update(
        {
            "backward_files": request.backward_files,
            "log_file": request.log_file,
            "result_path": request.result_path or "",
            "max_reschedule_times": request.max_reschedule_times or "",
            "max_run_time": request.max_run_time or "",
            "nnode": request.nnode or "",
            "dataset_paths": [],
            "extra_submit_args": list(request.extra_submit_args),
        }
    )
    workflow = request.to_dict()
    for key in (
        "workers",
        "image",
        "project_id",
        "machine_type",
        "out_dir",
        "job_prefix",
        "job_group_id",
        "job_group_name",
        "auto_job_group",
        "result_path",
        "backward_files",
        "log_file",
        "max_reschedule_times",
        "max_run_time",
        "nnode",
        "extra_submit_args",
    ):
        workflow.pop(key, None)
    workflow["kind"] = "dpa-berny"
    batch_config["workflow"] = workflow
    dump_toml(output / "batch.toml", batch_config)

    task_rows: list[dict[str, object]] = []
    for task in tasks:
        label = worker_label(task.worker, request.workers)
        worker_dir = output / label
        file_contents = render_task_files(request, task)
        for filename, contents in file_contents.items():
            (worker_dir / filename).write_text(contents, encoding="utf-8")
        with (worker_dir / "manifest.txt").open("a", encoding="utf-8") as manifest:
            manifest.write(f"{task.gjf_name}\n")
        json_record = ",".join(task.json_names)
        traj_record = ",".join(task.traj_names)
        with (worker_dir / "manifest.tsv").open("a", encoding="utf-8") as manifest:
            manifest.write(f"{task.source_xyz}\t{task.gjf_name}\t{json_record}\t{traj_record}\n")
        task_rows.append(
            {
                "worker": label,
                "source_xyz": str(task.source_xyz),
                "gjf": task.gjf_name,
                "json": json_record,
                "traj": traj_record,
            }
        )
        counts[task.worker] += 1

    _write_tsv(
        output / "tasks.tsv",
        ("worker", "source_xyz", "gjf", "json", "traj"),
        task_rows,
    )
    submit_rows = [
        {
            "worker": worker_label(index, request.workers),
            "tasks": counts[index],
            "job_name": f"{job_prefix}-{worker_label(index, request.workers)}",
            "input_dir": str(output / worker_label(index, request.workers)),
        }
        for index in range(request.workers)
    ]
    _write_tsv(output / "submit-plan.tsv", ("worker", "tasks", "job_name", "input_dir"), submit_rows)
    _prepared_results(output, submit_rows, "DRY_RUN" if dry_run else "PREPARED")
    return StagedRun(run_id, output, request.workers, len(tasks), tuple(counts), "dpa-berny")
