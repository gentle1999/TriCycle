from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tricycle_core import (
    BohrJobSpec,
    ExecutionContext,
    TaskPlan,
    TaskResult,
    TaskStatus,
)
from tricycle_core.errors import InvalidRequestError
from tricycle_core.protocols import BohrGateway


class BohrJobExecutor:
    """Execute a planned single Bohr job through an injected gateway.

    This is the shared transport helper for generic QM jobs and model jobs.
    It deliberately knows nothing about Gaussian, DPA-Berny, SAR, or RitS;
    those components only provide a serialized ``BohrJobSpec`` in their plan.
    """

    def submit_plan(
        self,
        plan: TaskPlan,
        gateway: BohrGateway,
        context: ExecutionContext,
        *,
        plugin_id: str,
        payload_key: str = "job",
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskResult:
        payload = plan.parameters.get(payload_key)
        if not isinstance(payload, Mapping):
            raise InvalidRequestError(f"Bohr task plan is missing its {payload_key} payload")
        job = BohrJobSpec.from_mapping(payload)
        return self.submit_job(
            job,
            gateway,
            context,
            task_id=plan.task_id,
            plugin_id=plugin_id,
            metadata=metadata,
        )

    def submit_job(
        self,
        job: BohrJobSpec,
        gateway: BohrGateway,
        context: ExecutionContext,
        *,
        task_id: str,
        plugin_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskResult:
        result_metadata = {"bohr_operation": job.operation, **dict(metadata or {})}
        if context.dry_run:
            return TaskResult(
                task_id=task_id,
                plugin_id=plugin_id,
                status=TaskStatus.PLANNED,
                message="dry-run: no external side effect was performed",
                metadata={**result_metadata, "context": context.to_dict()},
            )

        external_id = gateway.submit(job)
        return TaskResult(
            task_id=task_id,
            plugin_id=plugin_id,
            status=TaskStatus.SUBMITTED,
            external_id=external_id,
            metadata=result_metadata,
        )
