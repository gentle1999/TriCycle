from __future__ import annotations

import csv
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tricycle_core import (
    Artifact,
    ExecutionContext,
    PluginBase,
    PluginKind,
    PluginManifest,
    TaskPlan,
    TaskResult,
    TaskStatus,
)
from tricycle_core.errors import InvalidRequestError

from .config import dump_toml, load_toml
from .gateway import BohrWorkflowGateway
from .lifecycle import (
    download_run,
    job_for_row,
    read_run_config,
    status_run,
    terminate_run,
    validate_prepared_chemistry_resources,
    watch_run,
)
from .models import BatchRequest, DpaBernyRequest, RescueRequest
from .rescue import prepare_rescue
from .staging import stage_batch, stage_dpa_berny
from .training_protocol import validate_training_protocol


class BohrSubmitPlugin(PluginBase):
    """Migrated batch/DPA-Berny orchestration plugin.

    Planning is pure.  Local staging is explicit through :meth:`prepare`,
    while remote submission and lifecycle operations require an injected
    :class:`BohrWorkflowGateway` and a non-dry-run context.
    """

    _manifest = PluginManifest(
        plugin_id="tricycle.bohr.submit",
        display_name="Bohr batch and chemistry workflows",
        version="0.1.0",
        kind=PluginKind.BOHR_WORKFLOW,
        description=(
            "Prepare and operate Bohr batch jobs, including Gaussian and DPA-Berny "
            "worker directories, through an injected gateway."
        ),
        capabilities=(
            "batch.prepare",
            "batch.submit",
            "batch.status",
            "batch.download",
            "batch.terminate",
            "batch.watch",
            "dpa-berny.rescue",
            "dpa-berny.prepare",
        ),
        dependencies=("tricycle.bohr.compute",),
    )

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    def plan(
        self,
        operation: str,
        request: dict[str, Any],
        context: ExecutionContext,
    ) -> TaskPlan:
        if "only_succeeded" in request:
            raise InvalidRequestError("only_succeeded is not supported; downloads include every submitted worker")
        if operation == "batch.prepare":
            batch_request = BatchRequest.from_mapping(request)
            return TaskPlan(
                task_id=f"{context.run_id}/bohr-submit/batch",
                plugin_id=self.manifest.plugin_id,
                operation=operation,
                parameters={"request": batch_request.to_dict()},
                metadata={"workflow": "batch", "dry_run": context.dry_run},
            )
        if operation == "dpa-berny.prepare":
            dpa_berny_request = DpaBernyRequest.from_mapping(request)
            return TaskPlan(
                task_id=f"{context.run_id}/bohr-submit/dpa-berny",
                plugin_id=self.manifest.plugin_id,
                operation=operation,
                parameters={"request": dpa_berny_request.to_dict()},
                metadata={
                    "workflow": "dpa-berny",
                    "engine": dpa_berny_request.engine,
                    "dry_run": context.dry_run,
                },
            )
        if operation in {
            "batch.submit",
            "batch.status",
            "batch.download",
            "batch.terminate",
            "batch.watch",
        }:
            if "run" in request:
                raise InvalidRequestError("unknown batch request field: run; use run_dir")
            run_dir = str(request.get("run_dir", ""))
            if not run_dir.strip():
                raise InvalidRequestError("run_dir is required")
            return TaskPlan(
                task_id=f"{context.run_id}/bohr-submit/{operation}",
                plugin_id=self.manifest.plugin_id,
                operation=operation,
                parameters={"request": {**request, "run_dir": run_dir}},
                metadata={"workflow": "batch", "dry_run": context.dry_run},
            )
        if operation == "dpa-berny.rescue":
            rescue_request = RescueRequest.from_mapping(request)
            return TaskPlan(
                task_id=f"{context.run_id}/bohr-submit/rescue",
                plugin_id=self.manifest.plugin_id,
                operation=operation,
                parameters={"request": rescue_request.to_dict()},
                metadata={"workflow": "dpa-berny-rescue", "dry_run": context.dry_run},
            )
        raise InvalidRequestError(f"unsupported Bohr submit operation: {operation}")

    def prepare(
        self,
        operation: str,
        request: Mapping[str, Any],
        context: ExecutionContext,
    ) -> TaskResult:
        if operation == "batch.prepare":
            batch_request = BatchRequest.from_mapping(request)
            staged = stage_batch(
                batch_request,
                run_id=context.run_id,
                workdir=context.workdir,
                dry_run=context.dry_run,
            )
        elif operation == "dpa-berny.prepare":
            dpa_berny_request = DpaBernyRequest.from_mapping(request)
            staged = stage_dpa_berny(
                dpa_berny_request,
                run_id=context.run_id,
                workdir=context.workdir,
                dry_run=context.dry_run,
            )
        else:
            raise InvalidRequestError(f"unsupported preparation operation: {operation}")
        return TaskResult(
            task_id=f"{context.run_id}/bohr-submit/{staged.kind}",
            plugin_id=self.manifest.plugin_id,
            status=TaskStatus.PLANNED,
            outputs=(Artifact(f"{staged.run_id}-run", str(staged.run_dir), kind="directory"),),
            message="local run directory prepared; no remote submission was performed",
            metadata=staged.to_dict(),
        )

    def rescue(
        self,
        request: RescueRequest | Mapping[str, Any],
        context: ExecutionContext,
    ) -> TaskResult:
        parsed = (
            request if isinstance(request, RescueRequest) else RescueRequest.from_mapping(request)
        )
        staged = prepare_rescue(parsed, run_id=context.run_id, workdir=context.workdir)
        return TaskResult(
            task_id=f"{context.run_id}/bohr-submit/rescue",
            plugin_id=self.manifest.plugin_id,
            status=TaskStatus.PLANNED,
            outputs=(
                Artifact(f"{staged.run_id}-rescue-run", str(staged.run_dir), kind="directory"),
            ),
            message="rescue run prepared; completed tasks were not requeued",
            metadata=staged.to_dict(),
        )

    def submit_prepared(
        self,
        run_dir: Path,
        gateway: BohrWorkflowGateway,
        context: ExecutionContext,
        *,
        continue_on_error: bool = False,
    ) -> TaskResult:
        run_dir = run_dir.resolve()
        validate_training_protocol(run_dir)
        validate_prepared_chemistry_resources(run_dir)
        if context.dry_run:
            return TaskResult(
                task_id=f"{context.run_id}/bohr-submit/submit",
                plugin_id=self.manifest.plugin_id,
                status=TaskStatus.PLANNED,
                message="dry-run: prepared run was not submitted",
                metadata={"run_dir": str(run_dir)},
            )

        config_path = run_dir / "batch.toml"
        config = load_toml(config_path)
        env = read_run_config(run_dir)
        submit_rows = _read_tsv(run_dir / "submit-plan.tsv")
        existing_rows = (
            _read_tsv(run_dir / "submit-results.tsv")
            if (run_dir / "submit-results.tsv").is_file()
            else []
        )
        existing_by_worker = {
            row.get("worker", ""): row for row in existing_rows if row.get("worker", "")
        }
        group_id = str(env.get("job_group_id", ""))
        auto_job_group = env.get("auto_job_group", True)
        if isinstance(auto_job_group, str):
            auto_job_group = auto_job_group.strip().lower() in {"1", "true", "yes", "on"}
        if auto_job_group and not group_id:
            group_id = gateway.create_job_group(
                str(env.get("job_group_name", run_dir.name)), str(env["project_id"])
            )
            (run_dir / "job-group-id").write_text(f"{group_id}\n", encoding="utf-8")
            run_config = config.setdefault("run", {})
            if not isinstance(run_config, dict):
                raise InvalidRequestError("batch.toml [run] must be a table")
            run_config["job_group_id"] = group_id
            dump_toml(config_path, config)

        result_rows: list[dict[str, object]] = []
        submitted = failed = reused = skipped = 0
        for row in submit_rows:
            worker = row.get("worker", "")
            task_count = int(row.get("tasks", "0") or 0)
            result: dict[str, object] = {
                "worker": worker,
                "tasks": task_count,
                "job_id": "",
                "job_name": row.get("job_name", ""),
                "status": "",
                "input_dir": row.get("input_dir", ""),
            }
            previous = existing_by_worker.get(worker)
            if previous is not None and previous.get("job_id", ""):
                result["job_id"] = previous["job_id"]
                result["status"] = previous.get("status", "SUBMITTED")
                reused += 1
                result_rows.append(result)
                continue
            if task_count == 0:
                result["status"] = "SKIPPED_EMPTY"
                skipped += 1
                result_rows.append(result)
                continue
            try:
                job = job_for_row(run_dir, {**row, "status": "PREPARED"}, job_group_id=group_id)
                job_id = gateway.submit(job)
                result["job_id"] = job_id
                result["status"] = "SUBMITTED"
                submitted += 1
                _write_submit_results(run_dir, [*result_rows, result], submit_rows)
                (run_dir / worker / "submit.out").write_text(
                    f"JobId:  {job_id}\n", encoding="utf-8"
                )
            except Exception as exc:
                result["status"] = "FAILED"
                failed += 1
                (run_dir / worker / "submit.out").write_text(str(exc), encoding="utf-8")
                if not continue_on_error:
                    result_rows.append(result)
                    _write_submit_results(run_dir, result_rows, submit_rows)
                    raise
            result_rows.append(result)
        _write_submit_results(run_dir, result_rows, submit_rows)
        status = TaskStatus.FAILED if failed else TaskStatus.SUBMITTED
        return TaskResult(
            task_id=f"{context.run_id}/bohr-submit/submit",
            plugin_id=self.manifest.plugin_id,
            status=status,
            external_id=group_id or None,
            message=(f"submitted={submitted} reused={reused} skipped={skipped} failed={failed}"),
            metadata={
                "run_dir": str(run_dir),
                "submitted": submitted,
                "reused": reused,
                "skipped": skipped,
                "failed": failed,
            },
        )

    def status(
        self,
        run_dir: Path,
        gateway: BohrWorkflowGateway,
        context: ExecutionContext,
    ) -> TaskResult:
        if context.dry_run:
            return _planned_lifecycle_result(context, "status", run_dir)
        status_file, summary = status_run(run_dir.resolve(), gateway)
        return TaskResult(
            task_id=f"{context.run_id}/bohr-submit/status",
            plugin_id=self.manifest.plugin_id,
            status=TaskStatus.SUCCEEDED,
            outputs=(Artifact(f"{run_dir.name}-status", str(status_file), kind="file"),),
            metadata=summary.to_dict(),
        )

    def download(
        self,
        run_dir: Path,
        gateway: BohrWorkflowGateway,
        context: ExecutionContext,
        *,
        continue_on_error: bool = False,
        resume: bool = False,
    ) -> TaskResult:
        if context.dry_run:
            return _planned_lifecycle_result(context, "download", run_dir)
        result_file, summary = download_run(
            run_dir.resolve(),
            gateway,
            continue_on_error=continue_on_error,
            resume=resume,
        )
        return TaskResult(
            task_id=f"{context.run_id}/bohr-submit/download",
            plugin_id=self.manifest.plugin_id,
            status=TaskStatus.FAILED if summary.failed else TaskStatus.SUCCEEDED,
            outputs=(Artifact(f"{run_dir.name}-downloads", str(result_file), kind="file"),),
            metadata=summary.to_dict(),
        )

    def terminate(
        self,
        run_dir: Path,
        gateway: BohrWorkflowGateway,
        context: ExecutionContext,
        *,
        confirm: bool,
        all_incomplete: bool = False,
        wanted_results: tuple[str, ...] = ("RUNNING", "PENDING"),
        wanted_workers: tuple[str, ...] = (),
    ) -> TaskResult:
        if context.dry_run:
            return _planned_lifecycle_result(context, "terminate", run_dir)
        result_file = terminate_run(
            run_dir.resolve(),
            gateway,
            confirm=confirm,
            all_incomplete=all_incomplete,
            wanted_results=wanted_results,
            wanted_workers=wanted_workers,
        )
        terminate_rows = _read_tsv(result_file)
        failed = sum(row.get("action") == "FAILED" for row in terminate_rows)
        return TaskResult(
            task_id=f"{context.run_id}/bohr-submit/terminate",
            plugin_id=self.manifest.plugin_id,
            status=TaskStatus.FAILED if failed else TaskStatus.SUCCEEDED,
            outputs=(Artifact(f"{run_dir.name}-terminate", str(result_file), kind="file"),),
            metadata={"failed": failed, "terminated": len(terminate_rows) - failed},
        )

    def watch(
        self,
        run_dir: Path,
        gateway: BohrWorkflowGateway,
        context: ExecutionContext,
        *,
        interval: float = 60,
        max_iterations: int = 0,
        download_when_complete: bool = False,
    ) -> TaskResult:
        if context.dry_run:
            return _planned_lifecycle_result(context, "watch", run_dir)
        summary = watch_run(
            run_dir.resolve(),
            gateway,
            interval=interval,
            max_iterations=max_iterations,
            download_when_complete=download_when_complete,
        )
        return TaskResult(
            task_id=f"{context.run_id}/bohr-submit/watch",
            plugin_id=self.manifest.plugin_id,
            status=TaskStatus.SUCCEEDED,
            metadata=summary.to_dict(),
        )


def _planned_lifecycle_result(
    context: ExecutionContext, operation: str, run_dir: Path
) -> TaskResult:
    return TaskResult(
        task_id=f"{context.run_id}/bohr-submit/{operation}",
        plugin_id="tricycle.bohr.submit",
        status=TaskStatus.PLANNED,
        message="dry-run: no Bohr network or filesystem lifecycle operation was performed",
        metadata={"run_dir": str(run_dir)},
    )


def _read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise InvalidRequestError(f"TSV file not found: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter="\t")]


def _write_submit_results(
    run_dir: Path,
    result_rows: list[dict[str, object]],
    submit_rows: list[dict[str, str]],
) -> None:
    destination = run_dir / "submit-results.tsv"
    previous = _read_tsv(destination) if destination.exists() else []
    by_worker: dict[str, Mapping[str, object]] = {
        str(row.get("worker", "")): row for row in previous
    }
    by_worker.update({str(row.get("worker", "")): row for row in result_rows})
    rows: list[Mapping[str, object]] = []
    for source in submit_rows:
        row = by_worker.get(source.get("worker", ""))
        if row is None:
            row = {
                "worker": source.get("worker", ""),
                "tasks": source.get("tasks", ""),
                "job_id": "",
                "job_name": source.get("job_name", ""),
                "status": "PREPARED",
                "input_dir": source.get("input_dir", ""),
            }
        rows.append(row)
    temporary = destination.with_suffix(".tsv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer: csv.DictWriter[str] = csv.DictWriter(
            handle,
            fieldnames=("worker", "tasks", "job_id", "job_name", "status", "input_dir"),
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(destination)
