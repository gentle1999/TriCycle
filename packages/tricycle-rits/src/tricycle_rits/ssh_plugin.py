from __future__ import annotations

import hashlib
import re
from dataclasses import replace
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

from .ssh import RitsSshFinetuneRequest, RitsSshGateway


def _generated_job_id(run_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", run_id).strip("-._")[:80] or "rits"
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


class RitsSshPlugin(PluginBase):
    """Plan RitS SSH jobs and execute them through an injected gateway."""

    _manifest = PluginManifest(
        plugin_id="tricycle.rits.ssh",
        display_name="RitS SSH execution",
        version="0.1.0",
        kind=PluginKind.RITS,
        description=(
            "Stage a fine-tuning bundle on an internal GPU host over OpenSSH, "
            "execute it in a Docker container with GPU access, and inspect its lifecycle."
        ),
        capabilities=(
            "rits.ssh.finetune",
            "rits.ssh.status",
            "rits.ssh.logs",
            "rits.ssh.terminate",
            "rits.ssh.download",
        ),
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
        if operation not in {"submit", "status", "logs", "terminate", "download"}:
            raise InvalidRequestError(f"unsupported RitS SSH operation: {operation}")
        parsed = RitsSshFinetuneRequest.from_mapping(request, operation=operation)
        job_id = parsed.job_id
        if operation == "submit" and job_id is None:
            job_id = _generated_job_id(context.run_id)
            parsed = replace(parsed, job_id=job_id)
        if job_id is None:
            raise InvalidRequestError(f"job_id is required for SSH {operation}")

        inputs = (
            (Artifact("rits-training-bundle", parsed.bundle_dir or "", kind="directory"),)
            if operation == "submit"
            else ()
        )
        return TaskPlan(
            task_id=f"{context.run_id}/rits-ssh/{operation}/{job_id}",
            plugin_id=self.manifest.plugin_id,
            operation=operation,
            inputs=inputs,
            parameters={"ssh_request": parsed.to_dict(), "job_id": job_id},
            metadata={
                "transport": "docker-context-over-ssh",
                "target": parsed.target,
                "remote_dir": parsed.remote_job_dir(job_id),
                "authentication": "OpenSSH agent/config; no secret in request",
            },
        )

    def execute_remote(
        self,
        plan: TaskPlan,
        gateway: RitsSshGateway,
        context: ExecutionContext,
    ) -> TaskResult:
        if context.dry_run:
            return self.execute(plan, context)
        payload = plan.parameters.get("ssh_request")
        if not isinstance(payload, dict):
            raise InvalidRequestError("SSH task plan is missing ssh_request")
        request = RitsSshFinetuneRequest.from_mapping(payload, operation=plan.operation)
        job_id = str(plan.parameters.get("job_id", ""))
        if not job_id:
            raise InvalidRequestError("SSH task plan is missing job_id")

        if plan.operation == "submit":
            metadata = gateway.submit(request, job_id)
            return TaskResult(
                task_id=plan.task_id,
                plugin_id=self.manifest.plugin_id,
                status=TaskStatus.SUBMITTED,
                external_id=job_id,
                message="RitS training bundle staged and remote process started",
                metadata=metadata,
            )
        if plan.operation == "status":
            metadata = gateway.status(request, job_id)
            state = str(metadata.get("state", "")).lower()
            status = (
                TaskStatus.SUCCEEDED
                if state == "succeeded"
                else TaskStatus.FAILED
                if state in {"failed", "cancelled", "lost", "missing", "not_running"}
                else TaskStatus.RUNNING
            )
            return TaskResult(
                task_id=plan.task_id,
                plugin_id=self.manifest.plugin_id,
                status=status,
                external_id=job_id,
                message=f"RitS SSH job state: {state or 'unknown'}",
                metadata=metadata,
            )
        if plan.operation == "logs":
            log_tail = gateway.logs(request, job_id)
            return TaskResult(
                task_id=plan.task_id,
                plugin_id=self.manifest.plugin_id,
                status=TaskStatus.SUCCEEDED,
                external_id=job_id,
                message="RitS SSH training log tail retrieved",
                metadata={"log_tail": log_tail, "lines": request.log_lines},
            )
        if plan.operation == "terminate":
            metadata = gateway.terminate(request, job_id)
            return TaskResult(
                task_id=plan.task_id,
                plugin_id=self.manifest.plugin_id,
                status=TaskStatus.SUCCEEDED,
                external_id=job_id,
                message="RitS Docker container stopped",
                metadata=metadata,
            )
        if plan.operation == "download":
            paths = gateway.download(request, job_id)
            return TaskResult(
                task_id=plan.task_id,
                plugin_id=self.manifest.plugin_id,
                status=TaskStatus.SUCCEEDED,
                external_id=job_id,
                outputs=tuple(
                    Artifact(f"rits-output-{index}", path)
                    for index, path in enumerate(paths, start=1)
                ),
                message="RitS SSH outputs downloaded",
                metadata={"download_dir": request.download_dir, "paths": list(paths)},
            )
        raise InvalidRequestError(f"unsupported RitS SSH operation in plan: {plan.operation}")
