from __future__ import annotations

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
from tricycle_core.protocols import ReactionOptAdapter

from .models import ReactionConfig, ReactionConfigRequest, ReactionProfileRequest


class ReactionOptPlugin(PluginBase):
    _manifest = PluginManifest(
        plugin_id="tricycle.reactionopt",
        display_name="ReactionOpt reaction configuration",
        version="0.1.0",
        kind=PluginKind.REACTIONOPT,
        description="Generate versioned reaction-path configuration for the ReactionOpt adapter.",
        capabilities=(
            "reaction-config.validate",
            "reaction-config.generate",
            "reaction-config.export",
            "reactionopt.profile",
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
        if operation == "profile":
            return self.plan_profile(ReactionProfileRequest.from_mapping(request), context)
        if operation != "generate":
            raise InvalidRequestError(f"unsupported ReactionOpt operation: {operation}")
        return self.plan_generate(ReactionConfigRequest.from_mapping(request), context)

    def generate(self, request: ReactionConfigRequest) -> ReactionConfig:
        return ReactionConfig(
            schema_version="tricycle.reactionopt/v1",
            reaction_id=request.reaction_id,
            reaction_smiles=request.reaction_smiles,
            method=request.method,
            parameters=dict(request.parameters),
            metadata={"source": self.manifest.plugin_id, **request.metadata},
        )

    def plan_generate(self, request: ReactionConfigRequest, context: ExecutionContext) -> TaskPlan:
        config = self.generate(request)
        return TaskPlan(
            task_id=f"{context.run_id}/reactionopt/{request.reaction_id}",
            plugin_id=self.manifest.plugin_id,
            operation="generate",
            parameters={"config": config.to_dict(), "output_dir": request.output_dir},
            metadata={"schema_version": config.schema_version, "dry_run": context.dry_run},
        )

    def export(
        self,
        request: ReactionConfigRequest,
        adapter: ReactionOptAdapter,
        context: ExecutionContext,
    ) -> TaskResult:
        plan = self.plan_generate(request, context)
        if context.dry_run:
            return self.execute(plan, context)
        config = self.generate(request)
        destination = Path(request.output_dir) if request.output_dir else None
        locations = adapter.generate(config.to_dict(), destination)
        return TaskResult(
            task_id=plan.task_id,
            plugin_id=self.manifest.plugin_id,
            status=TaskStatus.SUCCEEDED,
            message="ReactionOpt configuration exported through adapter",
            metadata={"locations": list(locations), "schema_version": config.schema_version},
        )

    def plan_profile(
        self,
        request: ReactionProfileRequest,
        context: ExecutionContext,
    ) -> TaskPlan:
        """Plan independent reactant, product, and reaction-level RitS branches."""

        from .reaction_profile import Reaction
        from .rits import RitsInferenceConfig

        reaction = Reaction.from_rxn_smiles(request.reaction_smiles)
        rits = RitsInferenceConfig.from_mapping(request.rits)
        rits_jobs = reaction.plan_rits_jobs(
            rits=rits,
        )
        return TaskPlan(
            task_id=f"{context.run_id}/reactionopt/profile/{request.reaction_id}",
            plugin_id=self.manifest.plugin_id,
            operation="profile",
            inputs=rits.all_input_artifacts,
            parameters={
                "request": request.to_dict(),
                "reaction": reaction.to_dict(),
                "rits_jobs": list(rits_jobs),
            },
            metadata={
                "schema_version": "tricycle.reactionopt/profile-v1",
                "reactant_product_conformer_sampling": "independent",
                "ts_sampling": "rits.run_ts_pipeline",
                "legacy_product_backward_strategy": False,
                "ts_pipeline": rits.pipeline.to_dict(),
                "rits_job_count": len(rits_jobs),
                "dry_run": context.dry_run,
            },
        )

    def execute_profile(
        self,
        request: ReactionProfileRequest,
        sampler: Any,
        context: ExecutionContext,
    ) -> TaskResult:
        """Build a profile through an injected RitS sampler.

        ``sampler`` is deliberately supplied by the application.  It may run
        the configured RitS fork entrypoint locally or submit/poll a Bohr task; this
        component never discovers credentials or chooses a transport itself.
        """

        plan = self.plan_profile(request, context)
        if context.dry_run:
            return self.execute(plan, context)

        from .reaction_profile import Reaction
        from .rits import RitsInferenceConfig

        reaction = Reaction.from_rxn_smiles(request.reaction_smiles)
        rits = RitsInferenceConfig.from_mapping(request.rits)
        profile = reaction.build_profile(
            sampler,
            sampling=request.sampling,
            rits=rits,
        )
        output_dir = _profile_output_dir(request.output_dir, context)
        written: tuple[Path, ...] = ()
        if output_dir is not None:
            written = profile.save(output_dir, file_format="both")
        outputs = tuple(
            Artifact(
                artifact_id=f"reactionopt-profile-{request.reaction_id}",
                location=str(output_dir),
                kind="directory",
                media_type="application/x-reaction-profile",
            )
            for _ in (output_dir,)
            if output_dir is not None
        )
        return TaskResult(
            task_id=plan.task_id,
            plugin_id=self.manifest.plugin_id,
            status=TaskStatus.SUCCEEDED,
            outputs=outputs,
            message=(
                "mapped reaction profile built from independent reactant, product, and "
                "RitS branches with the TS generation/clustering/g-xTB pipeline"
            ),
            metadata={
                **profile.to_dict(),
                "written_files": [str(path) for path in written],
            },
        )


def _profile_output_dir(value: str | None, context: ExecutionContext) -> Path | None:
    if value is None or not str(value).strip():
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = context.workdir / path
    return path.resolve()
