from __future__ import annotations

from dataclasses import replace

from tricycle_bohr import BohrModelJobRequest, BohrModelPlugin
from tricycle_bohr.model_source import bind_model_source
from tricycle_core import (
    Artifact,
    ExecutionContext,
    PluginKind,
    PluginManifest,
    TaskPlan,
    TaskResult,
)
from tricycle_core.errors import InvalidRequestError
from tricycle_core.protocols import BohrGateway
from tricycle_core.run_paths import timestamped_run_directory

from .assets import load_model_asset
from .models import RxnResidJobRequest
from .sampling_runtime import binding
from .single_job import bind_request, prepare


class SarPlugin(BohrModelPlugin):
    """Plan RxnResid train/infer/finetune jobs under ``sar.*``."""

    request_type = RxnResidJobRequest
    model_family = "sar"

    _manifest = PluginManifest(
        plugin_id="tricycle.sar",
        display_name="RxnResid training and inference",
        version="0.1.0",
        kind=PluginKind.SAR,
        description=(
            "Build RxnResid train/inference jobs from the gentle1999/RxnResid repository "
            "and submit them to Bohr through the shared model job contract."
        ),
        capabilities=(
            "sar.train",
            "sar.infer",
            "sar.finetune",
            "sar.bohr-submit",
        ),
        dependencies=("tricycle.bohr.compute",),
    )

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    def plan_job(self, request: BohrModelJobRequest, context: ExecutionContext) -> TaskPlan:
        """Plan a job and pin its source provenance to the RxnResid repository."""

        plan = super().plan_job(request, context)
        asset = load_model_asset()
        plan = bind_model_source(
            plan,
            mode=request.mode,
            label="RxnResid",
            repository=asset.repository,
            revision=asset.revision,
            training_entrypoint=asset.training_entrypoint,
            inference_entrypoint=asset.inference_entrypoint,
            inference_protocol="predict",
            finetune_protocol=asset.finetune_protocol,
        )
        bohr_job = dict(plan.parameters["bohr_job"])
        job_parameters = dict(bohr_job["parameters"])
        job_parameters.setdefault("config_path", asset.config_path)
        job_parameters.setdefault("training_entrypoint", asset.training_entrypoint)
        job_parameters.setdefault("inference_entrypoint", asset.inference_entrypoint)
        job_parameters.setdefault("uncertainty_method", asset.uncertainty_method)
        job_parameters.setdefault("uncertainty_fields", list(asset.uncertainty_fields))
        record = bind_request(request, context)
        job_parameters["sar_backend"] = record
        job_parameters["backward_files"] = ["worker.log", record["result_path"]]
        bohr_job["machine_type"] = record["backend"]["environment"]["machine_type"]
        bohr_job["parameters"] = job_parameters
        return replace(
            plan,
            parameters={**plan.parameters, "bohr_job": bohr_job},
        )

    def submit_to_bohr(
        self, plan: TaskPlan, gateway: BohrGateway, context: ExecutionContext
    ) -> TaskResult:
        job = dict(plan.parameters["bohr_job"])
        record = job["parameters"].get("sar_backend")
        if not isinstance(record, dict):
            raise InvalidRequestError("SAR submission requires a shared backend binding")
        if record["backend"] != binding():
            raise InvalidRequestError("Planned SAR backend binding differs")
        if (
            job["image"] != record["backend"]["environment"]["image"]
            or job["machine_type"] != record["backend"]["environment"]["machine_type"]
            or list(job["command"]) != record["command"]
            or plan.operation != record["mode"]
        ):
            raise InvalidRequestError("SAR job differs from its planned backend binding")
        if context.dry_run:
            return super().submit_to_bohr(plan, gateway, context)
        supplied = job["parameters"].get("run_dir")
        output = (
            (context.workdir / str(supplied)).resolve()
            if supplied
            else timestamped_run_directory(context.workdir / "runs", context.run_id)
        )
        prepare(record, output)
        job.update(
            command=["bash", "worker.sh", "task.toml"],
            input_artifacts=[Artifact("sar-worker-inputs", str(output / "input-assets.json"), kind="manifest").to_dict()],
            parameters={
                **{key: value for key, value in job["parameters"].items() if key != "input_directory"},
                "input_assets_manifest": str(output / "input-assets.json"),
                "run_dir": str(output),
                "backward_files": ["worker.log", record["result_path"]],
            },
        )
        prepared = replace(plan, parameters={**plan.parameters, "bohr_job": job})
        return super().submit_to_bohr(prepared, gateway, context)
