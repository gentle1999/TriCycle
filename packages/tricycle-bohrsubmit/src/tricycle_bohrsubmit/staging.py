from __future__ import annotations

import glob
import json
import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tricycle_core import timestamped_run_directory
from tricycle_core.asset_store import put_tree
from tricycle_core.errors import InvalidRequestError
from tricycle_core.io import write_delimited, write_json
from tricycle_core.transfer import (
    validate_transfer_file,
    validate_transfer_path,
    validate_transfer_tree,
)

from .config import dump_toml
from .dpa import build_dpa_tasks, render_task_files
from .models import BatchRequest, DpaBernyRequest
from .task_table import expand_task_specs
from .training_protocol import bind_staged_training_protocol
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
    validate_transfer_path(source.name)
    if source.is_dir():
        validate_transfer_tree(source)
        shutil.copytree(source, destination)
    else:
        validate_transfer_file(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _link_staged_file(source: str | Path, destination: str | Path) -> str:
    """Reuse a run-owned immutable copy; never link to an external source."""
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return str(destination)


def _reuse_staged_path(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, copy_function=_link_staged_file)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _link_staged_file(source, destination)


def _write_env(path: Path, values: dict[str, object]) -> None:
    """Write shell variables consumed by the worker scripts."""

    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Runtime environment bridge; declarative configuration is in batch.toml.\n")
        for key, value in values.items():
            handle.write(f"{key}={shlex.quote(str(value))}\n")


def _write_tsv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    write_delimited(path, fieldnames, rows, delimiter="\t")


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

export DEVICE=cuda
mkdir -p results
python - <<'PY'
import json
from pathlib import Path
import torch
from deepmd.pt.utils import env

if not torch.cuda.is_available() or env.DEVICE.type != "cuda":
    raise RuntimeError("DPA requires CUDA; CPU fallback is disabled")
value = torch.ones((2, 2), device=env.DEVICE, requires_grad=True)
(value @ value).sum().backward()
torch.cuda.synchronize(env.DEVICE)
report = {"device": str(env.DEVICE), "device_name": torch.cuda.get_device_name(env.DEVICE),
          "torch": str(torch.__version__), "cuda": torch.version.cuda,
          "forward_backward_verified": True}
Path("results/dpa-device.json").write_text(json.dumps(report, indent=2) + "\\n")
print("[dpa-device] " + json.dumps(report), flush=True)
PY

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
        count = int(str(row["tasks"]))
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


def publish_worker_inputs(
    output: Path, workers: list[Path], config: dict, store: Path,
) -> None:
    """Persist only immutable input references in worker run records."""
    references = {worker.name: put_tree(worker, store) for worker in workers}
    config["input_assets"] = references
    for name, reference in references.items():
        record = output / name
        record.mkdir(parents=True, exist_ok=True)
        write_json(record / "input-assets.json", {
            "schema": "tricycle.asset-input/v1", "trees": [reference], "files": {},
        })


def stage_batch(request: BatchRequest, *, run_id: str, workdir: Path, dry_run: bool) -> StagedRun:
    with tempfile.TemporaryDirectory(prefix="tricycle-batch-prepare-") as temporary:
        return _stage_batch(request, run_id=run_id, workdir=workdir, dry_run=dry_run,
                            input_root=Path(temporary))


def _stage_batch(request: BatchRequest, *, run_id: str, workdir: Path, dry_run: bool,
                 input_root: Path) -> StagedRun:
    # Local staging is explicit; dry-run only suppresses remote effects.
    tasks = _expand_patterns(request.task_patterns)
    task_specs = expand_task_specs(request.task_defaults, request.task_specs)
    if not tasks and not task_specs:
        raise InvalidRequestError(f"no task files matched: {request.task_patterns}")
    binding = None
    for common in request.common_paths:
        if common.name == "training-protocol.json":
            value = json.loads(common.read_text())
            if "method_id" in value:
                binding = value
    output = (request.out_dir or timestamped_run_directory(workdir / "runs", run_id)).resolve()
    _assert_empty_output(output)

    job_prefix = compact_name(request.job_prefix or f"bohr-batch-{run_id}")
    job_group_name = compact_name(request.job_group_name or job_prefix)
    worker_dirs = _stage_common_worker_dirs(input_root, request.workers)
    counts = [0] * request.workers
    for index, worker_dir in enumerate(worker_dirs):
        (worker_dir / "tasks").mkdir()
        (worker_dir / "common").mkdir()
        if request.worker_script is not None:
            source = request.worker_script.resolve()
            if not source.is_file():
                raise InvalidRequestError(f"worker_script is not a file: {source}")
            _copy_path(source, worker_dir / "run_worker.sh")
        else:
            script_source = request.script.resolve() if request.script is not None else None
            if script_source is None or not script_source.is_file():
                raise InvalidRequestError(f"script is not a file: {script_source}")
            if binding is not None and index:
                _reuse_staged_path(
                    worker_dirs[0] / "common/__bohr_task_script.sh",
                    worker_dir / "common/__bohr_task_script.sh",
                )
            else:
                _copy_path(script_source, worker_dir / "common" / "__bohr_task_script.sh")
            (worker_dir / "run_worker.sh").write_text(_generic_worker_runner(), encoding="utf-8")
        (worker_dir / "run_worker.sh").chmod(0o755)

    task_rows: list[dict[str, object]] = []
    inputs: list[tuple[str, str, Path | None, dict[str, Any] | None]] = [
        (str(path), path.name, path, None) for path in tasks
    ]
    inputs += [(f"inline:{name}", f"{name}.toml", None, values) for name, values in task_specs]
    for index, (identity, name, task_path, values) in enumerate(inputs):
        worker = (
            index % request.workers
            if values is not None
            else posix_cksum(identity) % request.workers
        )
        label = worker_label(worker, request.workers)
        cloud_path = f"tasks/{short_hash(identity)}_{name}"
        if values is not None:
            dump_toml(input_root / label / cloud_path, values)
        else:
            if task_path is None:
                raise InvalidRequestError("Missing task input path")
            _copy_path(task_path, input_root / label / cloud_path)
        with (input_root / label / "manifest.txt").open("a", encoding="utf-8") as manifest:
            manifest.write(f"{cloud_path}\n")
        if values is None or "manifest.tsv" in request.backward_files.split(","):
            with (input_root / label / "manifest.tsv").open("a", encoding="utf-8") as manifest:
                manifest.write(f"{identity}\t{cloud_path}\n")
        task_rows.append({"worker": label, "source_path": identity, "cloud_path": cloud_path})
        counts[worker] += 1

    for index, worker_dir in enumerate(worker_dirs):
        # The common directory is populated after task staging so duplicate
        # names can be checked consistently for every worker.
        for common in request.common_paths:
            if binding is not None and common.name == "training-protocol.json":
                continue
            source = common.resolve()
            if not source.exists():
                raise InvalidRequestError(f"common path does not exist: {source}")
            destination = worker_dir / "common" / common.name
            if destination.exists():
                raise InvalidRequestError(f"duplicate staged common basename: {source.name}")
            if binding is not None and index:
                _reuse_staged_path(worker_dirs[0] / "common" / common.name, destination)
            else:
                _copy_path(source, destination)

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
    if any((directory / "common/training-protocol.json").exists() for directory in worker_dirs):
        batch_config["run"]["training_protocol_required"] = True
    publish_worker_inputs(output, worker_dirs, batch_config, workdir / "artifacts/task-assets")
    dump_toml(output / "batch.toml", batch_config)
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
    _write_tsv(
        output / "submit-plan.tsv", ("worker", "tasks", "job_name", "input_dir"), submit_rows
    )
    _prepared_results(output, submit_rows, "DRY_RUN" if dry_run else "PREPARED")
    if binding is not None:
        bind_staged_training_protocol(output, binding, batch_config)
    return StagedRun(run_id, output, request.workers, len(inputs), tuple(counts), "batch")


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
    request: DpaBernyRequest, *, run_id: str, workdir: Path, dry_run: bool
) -> StagedRun:
    with tempfile.TemporaryDirectory(prefix="tricycle-chemistry-prepare-") as temporary:
        return _stage_dpa_berny(request, run_id=run_id, workdir=workdir, dry_run=dry_run,
                                input_root=Path(temporary))


def _stage_dpa_berny(
    request: DpaBernyRequest, *, run_id: str, workdir: Path, dry_run: bool, input_root: Path
) -> StagedRun:
    tasks = build_dpa_tasks(request)
    if request.engine == "dpa":
        assert request.model_path is not None
        model_path = request.model_path.resolve()
        if not model_path.is_file():
            raise InvalidRequestError(f"model_path is not a file: {model_path}")
    else:
        model_path = None

    output = (request.out_dir or timestamped_run_directory(workdir / "runs", run_id)).resolve()
    _assert_empty_output(output)
    job_prefix = compact_name(request.job_prefix or f"dpa-berny-{run_id}")
    job_group_name = compact_name(request.job_group_name or job_prefix)
    worker_dirs = _stage_common_worker_dirs(input_root, request.workers)
    counts = [0] * request.workers
    for worker_dir in worker_dirs:
        (worker_dir / "work.sh").write_text(_chemistry_worker(request.engine), encoding="utf-8")
        (worker_dir / "work.sh").chmod(0o755)
        _write_env(worker_dir / "dpa-berny.env", _dpa_env(request))
        if model_path is not None:
            _copy_path(model_path, worker_dir / request.model_name)
            (worker_dir / "checkpoint").write_text(f"./{request.model_name}", encoding="utf-8")

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
        worker_dir = input_root / label
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
    _write_tsv(
        output / "submit-plan.tsv", ("worker", "tasks", "job_name", "input_dir"), submit_rows
    )
    _prepared_results(output, submit_rows, "DRY_RUN" if dry_run else "PREPARED")
    publish_worker_inputs(output, worker_dirs, batch_config, workdir / "artifacts/task-assets")
    dump_toml(output / "batch.toml", batch_config)
    return StagedRun(run_id, output, request.workers, len(tasks), tuple(counts), "dpa-berny")
