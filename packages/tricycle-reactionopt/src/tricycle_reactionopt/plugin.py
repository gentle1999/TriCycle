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
    timestamped_run_directory,
)
from tricycle_core.errors import InvalidRequestError
from tricycle_core.protocols import ReactionOptAdapter

from .diels_alder import enumerate_diels_alder_reactions, write_mapped_reaction_csv
from .models import (
    DielsAlderEnumerationRequest,
    ReactionConfig,
    ReactionConfigRequest,
    ReactionProfileRequest,
)


class ReactionOptPlugin(PluginBase):
    _manifest = PluginManifest(
        plugin_id="tricycle.reactionopt",
        display_name="ReactionOpt reaction workflows",
        version="0.1.0",
        kind=PluginKind.REACTIONOPT,
        description=(
            "Generate mapped Diels–Alder reaction candidates, ReactionOpt profiles, "
            "and versioned reaction-path configuration."
        ),
        capabilities=(
            "reaction-config.validate",
            "reaction-config.generate",
            "reaction-config.export",
            "reactionopt.profile",
            "reactionopt.enumerate-da",
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
        if operation == "enumerate-da":
            return self.plan_diels_alder_enumeration(
                DielsAlderEnumerationRequest.from_mapping(request),
                context,
                prefer_context_run_dir=(request.get("output_dir") is None),
            )
        if operation == "profile":
            return self.plan_profile(ReactionProfileRequest.from_mapping(request), context)
        if operation != "generate":
            raise InvalidRequestError(f"unsupported ReactionOpt operation: {operation}")
        return self.plan_generate(ReactionConfigRequest.from_mapping(request), context)

    def plan_diels_alder_enumeration(
        self,
        request: DielsAlderEnumerationRequest,
        context: ExecutionContext,
        *,
        prefer_context_run_dir: bool = False,
    ) -> TaskPlan:
        destination = _enumeration_output_path(
            request, context, prefer_context_run_dir=prefer_context_run_dir
        )
        return TaskPlan(
            task_id=f"{context.run_id}/reactionopt/enumerate-da",
            plugin_id=self.manifest.plugin_id,
            operation="enumerate-da",
            parameters={
                "request": request.to_dict(),
                "output_csv": str(destination),
            },
            metadata={
                "schema_version": "tricycle.reactionopt/da-candidates-v1",
                "substrate_pair_count": len(request.pairs),
                "stereochemistry": request.stereochemistry,
                "dry_run": context.dry_run,
            },
        )

    def execute_diels_alder_enumeration(
        self,
        request: DielsAlderEnumerationRequest,
        context: ExecutionContext,
        plan: TaskPlan | None = None,
    ) -> TaskResult:
        plan = plan or self.plan_diels_alder_enumeration(request, context)
        enumeration = enumerate_diels_alder_reactions(request)
        destination = write_mapped_reaction_csv(
            enumeration.candidates,
            Path(str(plan.parameters["output_csv"])),
        )
        return TaskResult(
            task_id=plan.task_id,
            plugin_id=self.manifest.plugin_id,
            status=TaskStatus.SUCCEEDED,
            outputs=(
                Artifact(
                    artifact_id="mapped-reactions",
                    location=str(destination),
                    media_type="text/csv",
                    metadata={
                        "candidate_count": len(enumeration.candidates),
                        "complete": enumeration.complete,
                    },
                ),
            ),
            message="mapped Diels–Alder reaction candidates enumerated",
            metadata={
                "candidate_count": len(enumeration.candidates),
                "substrate_pair_count": enumeration.substrate_pair_count,
                "enumeration_complete": enumeration.complete,
                "stereochemistry": request.stereochemistry,
                "output_csv": str(destination),
            },
        )

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


def _enumeration_output_path(
    request: DielsAlderEnumerationRequest,
    context: ExecutionContext,
    *,
    prefer_context_run_dir: bool = False,
) -> Path:
    if request.output_csv is not None:
        path = Path(request.output_csv)
        if not path.is_absolute():
            path = context.workdir / path
        return path.resolve()

    run_dir = context.metadata.get("run_dir")
    if prefer_context_run_dir and run_dir is not None:
        output_dir = Path(str(run_dir))
        if not output_dir.is_absolute():
            output_dir = context.workdir / output_dir
    elif request.output_dir is not None:
        output_dir = Path(request.output_dir)
        if not output_dir.is_absolute():
            output_dir = context.workdir / output_dir
    else:
        if run_dir is None:
            output_dir = timestamped_run_directory(
                context.workdir / "runs", context.run_id
            )
        else:
            output_dir = Path(str(run_dir))
            if not output_dir.is_absolute():
                output_dir = context.workdir / output_dir
    return (output_dir / "mapped-reactions.csv").resolve()
