from __future__ import annotations

from tricycle_bohr import BohrModelJobRequest, BohrModelPlugin
from tricycle_bohr.model_source import bind_model_source
from tricycle_core import ExecutionContext, PluginKind, PluginManifest, TaskPlan

from .assets import load_model_asset
from .models import RitsJobRequest


class RitsPlugin(BohrModelPlugin):
    """Plan RitS train/infer/finetune jobs and delegate submission to Bohr."""

    request_type = RitsJobRequest
    model_family = "rits"

    _manifest = PluginManifest(
        plugin_id="tricycle.rits",
        display_name="RitS training and inference",
        version="0.1.0",
        kind=PluginKind.RITS,
        description=(
            "Build RitS train/inference jobs and submit them to Bohr through "
            "the shared model job contract."
        ),
        capabilities=(
            "rits.train",
            "rits.infer",
            "rits.finetune",
            "rits.bohr-submit",
        ),
        dependencies=("tricycle.bohr.compute",),
    )

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    def plan_job(self, request: BohrModelJobRequest, context: ExecutionContext) -> TaskPlan:
        """Plan a job and pin its source provenance to the selected RitS fork."""

        plan = super().plan_job(request, context)
        asset = load_model_asset()
        return bind_model_source(plan, mode=request.mode, label="RitS", repository=asset.repository,
            revision=asset.revision, training_entrypoint=asset.training_entrypoint,
            inference_entrypoint=asset.inference_entrypoint, inference_protocol="sample_transition_state",
            finetune_protocol=asset.finetune_protocol)
