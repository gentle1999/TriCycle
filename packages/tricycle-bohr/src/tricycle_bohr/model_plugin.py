from __future__ import annotations

from typing import Any, ClassVar

from tricycle_core import (
    ExecutionContext,
    ModelMode,
    PluginBase,
    TaskPlan,
    TaskResult,
)
from tricycle_core.errors import InvalidRequestError
from tricycle_core.protocols import BohrGateway

from .executor import BohrJobExecutor
from .models import BohrModelJobRequest


class BohrModelPlugin(PluginBase):
    """Common component implementation for one-shot Bohr model jobs.

    Domain packages provide only a request class and a manifest.  The
    train/infer/finetune mode mapping, TaskPlan shape, and Bohr submission path stay
    shared with every model component that uses the single-job gateway.
    """

    request_type: ClassVar[type[BohrModelJobRequest]] = BohrModelJobRequest
    model_family: ClassVar[str] = "model"

    def plan(
        self,
        operation: str,
        request: dict[str, Any],
        context: ExecutionContext,
    ) -> TaskPlan:
        mode = {
            "train": ModelMode.TRAIN,
            "infer": ModelMode.INFER,
            "finetune": ModelMode.FINETUNE,
            "fine-tune": ModelMode.FINETUNE,
            "fine_tune": ModelMode.FINETUNE,
            "fine-tuning": ModelMode.FINETUNE,
            "fine_tuning": ModelMode.FINETUNE,
        }.get(operation)
        if mode is None:
            raise InvalidRequestError(f"unsupported {self.model_family} operation: {operation}")
        return self.plan_job(self.request_type.from_mapping(request, mode), context)

    def plan_training(
        self,
        request: BohrModelJobRequest,
        context: ExecutionContext,
    ) -> TaskPlan:
        if request.mode is not ModelMode.TRAIN:
            raise InvalidRequestError("plan_training requires mode=train")
        return self.plan_job(request, context)

    def plan_inference(
        self,
        request: BohrModelJobRequest,
        context: ExecutionContext,
    ) -> TaskPlan:
        if request.mode is not ModelMode.INFER:
            raise InvalidRequestError("plan_inference requires mode=infer")
        return self.plan_job(request, context)

    def plan_finetune(
        self,
        request: BohrModelJobRequest,
        context: ExecutionContext,
    ) -> TaskPlan:
        if request.mode is not ModelMode.FINETUNE:
            raise InvalidRequestError("plan_finetune requires mode=finetune")
        return self.plan_job(request, context)

    def plan_fine_tuning(
        self,
        request: BohrModelJobRequest,
        context: ExecutionContext,
    ) -> TaskPlan:
        """Compatibility spelling for :meth:`plan_finetune`."""

        return self.plan_finetune(request, context)

    def plan_job(self, request: BohrModelJobRequest, context: ExecutionContext) -> TaskPlan:
        if request.model_family != self.model_family:
            raise InvalidRequestError(
                f"request model family does not match plugin: "
                f"{request.model_family} != {self.model_family}"
            )
        job = request.to_bohr_job()
        return TaskPlan(
            task_id=f"{context.run_id}/{self.model_family}/{request.mode.value}/{request.model_name}",
            plugin_id=self.manifest.plugin_id,
            operation=request.mode.value,
            inputs=job.input_artifacts,
            parameters={"bohr_job": job.to_dict()},
            metadata={
                "bohr_target": True,
                "model_family": self.model_family,
                "mode": request.mode.value,
            },
        )

    def submit_to_bohr(
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
            payload_key="bohr_job",
            metadata={
                "bohr_target": True,
                "model_family": self.model_family,
            },
        )
