from __future__ import annotations

from dataclasses import replace

from tricycle_bohr import BohrModelJobRequest, BohrModelPlugin
from tricycle_core import ExecutionContext, PluginKind, PluginManifest, TaskPlan
from tricycle_core.errors import InvalidRequestError

from .assets import load_default_model_asset
from .models import RxnResidJobRequest


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
        asset = load_default_model_asset()
        bohr_job = dict(plan.parameters["bohr_job"])
        job_parameters = dict(bohr_job["parameters"])
        for key, expected in (
            ("source_repository", asset.repository),
            ("source_revision", asset.revision),
            (
                "upstream_entrypoint",
                {
                    "train": asset.training_entrypoint,
                    "infer": asset.inference_entrypoint,
                    "finetune": asset.training_entrypoint,
                }[request.mode.value],
            ),
            (
                "upstream_protocol",
                {
                    "train": "train",
                    "infer": "predict",
                    "finetune": asset.finetune_protocol,
                }[request.mode.value],
            ),
        ):
            supplied = job_parameters.get(key)
            if supplied is not None and (
                not isinstance(supplied, str) or supplied.strip() != expected
            ):
                raise InvalidRequestError(
                    f"RxnResid {key} must be {expected!r} for the configured upstream repository"
                )
            job_parameters[key] = expected
        job_parameters.setdefault("config_path", asset.config_path)
        job_parameters.setdefault("training_entrypoint", asset.training_entrypoint)
        job_parameters.setdefault("inference_entrypoint", asset.inference_entrypoint)
        job_parameters.setdefault("uncertainty_method", asset.uncertainty_method)
        job_parameters.setdefault("uncertainty_fields", list(asset.uncertainty_fields))
        bohr_job["parameters"] = job_parameters
        return replace(
            plan,
            parameters={**plan.parameters, "bohr_job": bohr_job},
            metadata={
                **plan.metadata,
                "source_repository": asset.repository,
                "source_revision": asset.revision,
                "uncertainty_method": asset.uncertainty_method,
                "uncertainty_fields": list(asset.uncertainty_fields),
            },
        )


class RxnResidPlugin(SarPlugin):
    """Named RxnResid entry point; ``SarPlugin`` remains the registry alias."""
