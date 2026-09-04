from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tricycle_core import InvalidRequestError, RunStore, TaskResult, TaskStatus, load_dotenv

_PREPARATION_CAPABILITIES = frozenset(
    {"bohr.batch.prepare", "dpa-berny.prepare", "dpa-berny.rescue"}
)
_BATCH_CAPABILITIES = frozenset(
    {
        "bohr.batch.submit",
        "bohr.batch.status",
        "bohr.batch.download",
        "bohr.batch.watch",
        "bohr.batch.terminate",
    }
)
_SUCCESSFUL_REMOTE_STATUSES = frozenset(
    {"finish", "finished", "success", "succeeded", "complete", "completed", "done"}
)
_FAILED_REMOTE_STATUSES = frozenset(
    {
        "fail",
        "failed",
        "error",
        "stopped",
        "terminated",
        "killed",
        "cancel",
        "canceled",
        "cancelled",
    }
)
_RETRYABLE_CAPABILITIES = frozenset(
    {
        "bohr.compute.submit",
        "bohr.job.status",
        "bohr.job.download",
        "bohr.batch.submit",
        "bohr.batch.status",
        "bohr.batch.download",
        "bohr.batch.watch",
        "bohr.batch.terminate",
        "reaction-db.batch-import",
        "reaction-db.filter",
        "reaction-db.export-rits",
        "reaction-db.export-rxnresid",
        "sar.train",
        "sar.infer",
        "sar.finetune",
        "rits.train",
        "rits.infer",
        "rits.finetune",
    }
)


def requested_run_dir(request: Mapping[str, Any], workdir: Path, run_id: str) -> Path:
    value = request.get("run_dir") or request.get("run")
    if value is not None and str(value).strip():
        return Path(str(value)).resolve()
    output = request.get("out_dir") or request.get("out")
    if output is not None and str(output).strip():
        return Path(str(output)).resolve()
    load_dotenv(keys=("TRICYCLE_RUN_ROOT",))
    run_root = os.environ.get("TRICYCLE_RUN_ROOT", "runs")
    return (workdir / run_root / run_id).resolve()


def copy_request(request_path: Path, run_dir: Path) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    destination = run_dir / "request.toml"
    source = request_path.resolve()
    if destination.exists():
        if destination.resolve() != source:
            existing = destination.read_bytes()
            if existing != source.read_bytes():
                raise InvalidRequestError(f"run request is immutable and differs: {destination}")
        return destination
    shutil.copy2(source, destination)
    return destination


def open_or_create(
    run_dir: Path,
    *,
    run_id: str,
    capability_id: str,
    dry_run: bool,
) -> RunStore:
    state_path = run_dir.resolve() / "run.toml"
    if state_path.is_file():
        return RunStore.open(run_dir)
    return RunStore.create(
        run_dir,
        run_id=run_id,
        capability_id=capability_id,
        dry_run=dry_run,
    )


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _remote_status(metadata: Mapping[str, Any]) -> str:
    return (
        str(metadata.get("status") or metadata.get("job_status") or "")
        .lower()
        .replace("_", "")
        .replace("-", "")
        .replace(" ", "")
    )


def next_action_for(capability_id: str, result: TaskResult) -> str:
    if result.status is TaskStatus.FAILED:
        return capability_id if capability_id in _RETRYABLE_CAPABILITIES else ""
    if capability_id in {"bohr.batch.prepare", "dpa-berny.prepare", "dpa-berny.rescue"}:
        return "bohr.batch.submit"
    if capability_id == "bohr.batch.submit":
        return "bohr.batch.status" if result.status is not TaskStatus.PLANNED else capability_id
    if capability_id in {"bohr.batch.status", "bohr.batch.watch"}:
        if result.status is TaskStatus.PLANNED:
            return capability_id
        if _as_int(result.metadata.get("incomplete")) > 0:
            return capability_id
        return "bohr.batch.download"
    if capability_id == "bohr.batch.download":
        return ""
    if capability_id == "bohr.batch.terminate":
        return ""
    if capability_id in {
        "reaction-db.batch-import",
        "reaction-db.filter",
        "reaction-db.export-rits",
        "reaction-db.export-rxnresid",
    }:
        return ""
    if capability_id in {
        "bohr.compute.submit",
        "sar.train",
        "sar.infer",
        "sar.finetune",
        "rits.train",
        "rits.infer",
        "rits.finetune",
    }:
        return "bohr.job.status" if result.external_id else ""
    if capability_id == "bohr.job.status":
        normalized = _remote_status(result.metadata)
        if normalized in _SUCCESSFUL_REMOTE_STATUSES:
            return "bohr.job.download"
        if normalized in _FAILED_REMOTE_STATUSES:
            return ""
        if normalized:
            return capability_id
    return ""


def state_for_result(capability_id: str, result: TaskResult) -> tuple[str, str]:
    if result.status is TaskStatus.FAILED:
        return "failed", "error"
    if capability_id in _PREPARATION_CAPABILITIES and result.status is not TaskStatus.FAILED:
        return "prepared", "local"
    if result.status is TaskStatus.PLANNED:
        return "planned", "plan"
    if capability_id == "bohr.batch.terminate" and result.status is not TaskStatus.FAILED:
        return "stopped", "remote"
    if capability_id == "bohr.batch.download" and result.status is not TaskStatus.FAILED:
        return "downloaded", "local"
    if capability_id == "bohr.job.download" and result.status is not TaskStatus.FAILED:
        return "downloaded", "local"
    if capability_id == "bohr.job.status" and result.status is not TaskStatus.FAILED:
        normalized = _remote_status(result.metadata)
        if normalized in _SUCCESSFUL_REMOTE_STATUSES:
            return "succeeded", "remote"
        if normalized in _FAILED_REMOTE_STATUSES:
            return "failed", "remote"
        if normalized:
            return "running", "remote"
        return "planned", "remote"
    if capability_id in {"bohr.batch.status", "bohr.batch.watch"}:
        if _as_int(result.metadata.get("failed")) > 0:
            return "failed", "remote"
        return (
            ("running", "remote")
            if _as_int(result.metadata.get("incomplete")) > 0
            else ("succeeded", "remote")
        )
    if result.status is TaskStatus.SUBMITTED:
        return "submitted", "remote"
    if result.status is TaskStatus.RUNNING:
        return "running", "remote"
    if result.status is TaskStatus.SUCCEEDED:
        return "succeeded", "completed"
    return "planned", "plan"


def record_result(
    store: RunStore,
    capability_id: str,
    result: TaskResult,
    *,
    next_action: str | None = None,
) -> dict[str, Any]:
    status, phase = state_for_result(capability_id, result)
    action = next_action if next_action is not None else next_action_for(capability_id, result)
    current = store.read()
    execution = current.get("execution", {})
    attempt = _as_int(execution.get("attempt")) + 1 if isinstance(execution, dict) else 1
    remote: dict[str, Any] = {}
    if result.external_id:
        remote["external_id"] = result.external_id
        if capability_id.startswith("bohr.batch") or capability_id.startswith("dpa-berny"):
            remote["job_group_id"] = result.external_id
        elif capability_id.startswith(("bohr.", "sar.", "rits.")):
            remote["job_id"] = result.external_id
        else:
            remote["record_id"] = result.external_id
    output_locations = [artifact.location for artifact in result.outputs]
    artifacts = {"last_outputs": output_locations}
    return store.update(
        status=status,
        phase=phase,
        next_action=action,
        last_successful_action=capability_id if result.status is not TaskStatus.FAILED else None,
        result=result,
        attempt=attempt,
        action=capability_id,
        remote=remote,
        artifacts=artifacts,
    )


def resume_request(store: RunStore) -> tuple[str, dict[str, Any]]:
    data = store.read()
    resume = data.get("resume", {})
    if not isinstance(resume, dict):
        raise InvalidRequestError(f"run.toml [resume] must be a table: {store.path}")
    capability_id = str(resume.get("next_action", ""))
    if not capability_id.strip():
        raise InvalidRequestError(f"run has no next action: {store.path}")

    run = data.get("run", {})
    if not isinstance(run, dict):
        raise InvalidRequestError(f"run.toml [run] must be a table: {store.path}")
    request_path = store.run_dir / "request.toml"
    if not request_path.is_file():
        raise InvalidRequestError(f"run request not found: {request_path}")

    from .toml_requests import load_request

    request = load_request(request_path)
    if capability_id in _BATCH_CAPABILITIES:
        # Keep lifecycle options such as only_succeeded, interval, and
        # continue_on_error when a run is resumed from its state directory.
        request["run_dir"] = str(store.run_dir)
        return capability_id, request
    remote = data.get("remote", {})
    if capability_id in {"bohr.job.status", "bohr.job.download"}:
        if not isinstance(remote, dict):
            raise InvalidRequestError(f"run.toml [remote] must be a table: {store.path}")
        external_id = str(remote.get("job_id", remote.get("external_id", "")))
        if not external_id.strip():
            raise InvalidRequestError(f"run has no remote job id: {store.path}")
        request = {"external_id": external_id}
        if capability_id == "bohr.job.download":
            request["destination"] = str(store.run_dir / "downloads")
        return capability_id, request

    return capability_id, load_request(request_path)
