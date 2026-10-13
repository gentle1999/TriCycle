from __future__ import annotations

from pathlib import Path
from typing import Any

from tricycle_core import (
    ExecutionContext,
    PluginBase,
    PluginKind,
    PluginManifest,
    TaskPlan,
    TaskResult,
    TaskStatus,
)
from tricycle_core.errors import InvalidRequestError
from tricycle_core.protocols import BohrGateway
from tricycle_core.validation import validate_fields

from .executor import BohrJobExecutor
from .models import BohrComputeRequest


class BohrComputePlugin(PluginBase):
    _manifest = PluginManifest(
        plugin_id="tricycle.bohr.compute",
        display_name="Bohr platform compute",
        version="0.1.0",
        kind=PluginKind.BOHR_COMPUTE,
        description="Plan and submit computational chemistry jobs to Bohr through an adapter.",
        capabilities=("compute.plan", "compute.submit", "compute.status", "compute.download"),
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
        if operation == "status":
            validate_fields(request, {"external_id"}, context="Bohr status")
            external_id = str(request.get("external_id", ""))
            if not external_id.strip():
                raise InvalidRequestError("external_id is required for Bohr status")
            return TaskPlan(
                task_id=f"{context.run_id}/bohr/status/{external_id}",
                plugin_id=self.manifest.plugin_id,
                operation=operation,
                parameters={"external_id": external_id},
                metadata={"dry_run": context.dry_run},
            )
        if operation == "download":
            validate_fields(request, {"external_id", "destination"}, context="Bohr download")
            external_id = str(request.get("external_id", ""))
            destination = str(request.get("destination", ""))
            if not external_id.strip() or not destination.strip():
                raise InvalidRequestError(
                    "external_id and destination are required for Bohr download"
                )
            return TaskPlan(
                task_id=f"{context.run_id}/bohr/download/{external_id}",
                plugin_id=self.manifest.plugin_id,
                operation=operation,
                parameters={"external_id": external_id, "destination": destination},
                metadata={"dry_run": context.dry_run},
            )
        if operation != "compute":
            raise InvalidRequestError(f"unsupported Bohr operation: {operation}")
        return self.plan_compute(BohrComputeRequest.from_mapping(request), context)

    def plan_compute(self, request: BohrComputeRequest, context: ExecutionContext) -> TaskPlan:
        job = request.to_job_spec()
        return TaskPlan(
            task_id=f"{context.run_id}/bohr/{job.name}",
            plugin_id=self.manifest.plugin_id,
            operation="compute",
            inputs=job.input_artifacts,
            parameters={"job": job.to_dict()},
            metadata={"bohr_operation": job.operation, "dry_run": context.dry_run},
        )

    def submit(
        self,
        plan: TaskPlan,
        gateway: BohrGateway,
        context: ExecutionContext,
    ) -> TaskResult:
        return BohrJobExecutor().submit_plan(
            plan,
            gateway,
            context,
            plugin_id=self.manifest.plugin_id,
        )

    def status(
        self,
        plan: TaskPlan,
        gateway: BohrGateway,
        context: ExecutionContext,
    ) -> TaskResult:
        if context.dry_run:
            return self.execute(plan, context)
        external_id = str(plan.parameters.get("external_id", ""))
        payload = gateway.status(external_id)
        return TaskResult(
            task_id=plan.task_id,
            plugin_id=self.manifest.plugin_id,
            status=TaskStatus.SUCCEEDED,
            external_id=external_id,
            message="Bohr job status queried",
            metadata={"status": payload.get("status", "UNKNOWN"), **payload},
        )

    def download(
        self,
        plan: TaskPlan,
        gateway: BohrGateway,
        context: ExecutionContext,
    ) -> TaskResult:
        if context.dry_run:
            return self.execute(plan, context)
        external_id = str(plan.parameters.get("external_id", ""))
        destination = Path(str(plan.parameters.get("destination", "")))
        locations = gateway.download(external_id, destination)
        return TaskResult(
            task_id=plan.task_id,
            plugin_id=self.manifest.plugin_id,
            status=TaskStatus.SUCCEEDED,
            external_id=external_id,
            message="Bohr job artifacts downloaded",
            metadata={"destination": str(destination), "locations": list(locations)},
        )
