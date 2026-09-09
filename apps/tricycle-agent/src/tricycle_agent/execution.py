from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tricycle_bohr import BohrComputePlugin
from tricycle_bohrsubmit import (
    BohrCliConfig,
    BohrCliGateway,
    BohrSubmitPlugin,
    InMemoryBohrGateway,
)
from tricycle_core import (
    CapabilityCatalog,
    ExecutionContext,
    IntegrationNotConfiguredError,
    InvalidRequestError,
    SideEffectLevel,
    TaskPlan,
    TaskResult,
    TaskStatus,
)
from tricycle_core.protocols import BohrGateway, ReactionOptAdapter, ReactionRepository
from tricycle_reaction_db import (
    InMemoryReactionRepository,
    ReactionBatchImportRequest,
    ReactionDatabaseAdapter,
    ReactionDatabasePlugin,
    ReactionFilterRequest,
    ReactionIngestRequest,
    ReactionSpaceExportRequest,
    RitsDatasetExportRequest,
    RxnResidDatasetExportRequest,
)
from tricycle_reactionopt import (
    ReactionConfigRequest,
    ReactionOptPlugin,
    ReactionProfileRequest,
)
from tricycle_rits import RitsPlugin
from tricycle_sampling import SamplingPlugin, SamplingRequest
from tricycle_sar import SarPlugin


def _run_dir(request: Mapping[str, Any]) -> Path:
    value = request.get("run_dir") or request.get("run") or ""
    if not str(value).strip():
        raise InvalidRequestError("run_dir is required")
    return Path(str(value))


class CapabilityDispatcher:
    """Execute public capabilities without exposing component internals to callers."""

    def __init__(
        self,
        catalog: CapabilityCatalog,
        *,
        bohr_gateway: BohrGateway | None = None,
        reaction_repository: ReactionRepository | None = None,
        reaction_database_adapter: ReactionDatabaseAdapter | None = None,
        reactionopt_adapter: ReactionOptAdapter | None = None,
        reactionopt_profile_adapter: Any | None = None,
    ) -> None:
        self.catalog = catalog
        self.bohr_gateway = bohr_gateway
        self.reaction_repository = reaction_repository
        self.reaction_database_adapter = reaction_database_adapter
        self.reactionopt_adapter = reactionopt_adapter
        self.reactionopt_profile_adapter = reactionopt_profile_adapter

    def plan(
        self,
        capability_id: str,
        request: Mapping[str, Any],
        context: ExecutionContext,
    ) -> TaskPlan:
        return self.catalog.plan(capability_id, request, context)

    def validate(
        self,
        capability_id: str,
        request: Mapping[str, Any],
        context: ExecutionContext,
    ) -> None:
        self.catalog.validate(capability_id, request, context)

    def execute(
        self,
        capability_id: str,
        request: Mapping[str, Any],
        context: ExecutionContext,
        *,
        confirm: bool = False,
    ) -> TaskResult:
        binding = self.catalog.get(capability_id)
        if (
            not context.dry_run
            and binding.manifest.side_effect is SideEffectLevel.DESTRUCTIVE
            and not confirm
        ):
            raise InvalidRequestError(
                f"{capability_id} requires explicit confirmation before execution"
            )
        result = self._execute(capability_id, request, context, confirm=confirm)
        return TaskResult(
            task_id=result.task_id,
            plugin_id=result.plugin_id,
            status=result.status,
            outputs=result.outputs,
            external_id=result.external_id,
            message=result.message,
            metadata={
                **result.metadata,
                "capability_id": capability_id,
                "side_effect": binding.manifest.side_effect.value,
            },
        )

    def _execute(
        self,
        capability_id: str,
        request: Mapping[str, Any],
        context: ExecutionContext,
        *,
        confirm: bool = False,
    ) -> TaskResult:
        binding = self.catalog.get(capability_id)
        provider = binding.provider
        planned = binding.plan(request, context)

        if capability_id == "reactionopt.generate":
            component = self._as(provider, ReactionOptPlugin)
            parsed = ReactionConfigRequest.from_mapping(request)
            config = component.generate(parsed)
            return TaskResult(
                task_id=f"{context.run_id}/reactionopt/{parsed.reaction_id}",
                plugin_id=provider.manifest.plugin_id,
                status=TaskStatus.SUCCEEDED,
                message="ReactionOpt configuration generated",
                metadata={"capability_id": capability_id, "config": config.to_dict()},
            )

        if capability_id == "candidate.select":
            component = self._as(provider, SamplingPlugin)
            parsed = SamplingRequest.from_mapping(request)
            result = component.select(parsed)
            return TaskResult(
                task_id=f"{context.run_id}/sampling/{parsed.strategy}",
                plugin_id=provider.manifest.plugin_id,
                status=TaskStatus.SUCCEEDED,
                message="reaction candidates selected",
                metadata={"capability_id": capability_id, **result.to_dict()},
            )

        if capability_id == "reactionopt.export":
            component = self._as(provider, ReactionOptPlugin)
            parsed = ReactionConfigRequest.from_mapping(request)
            plan = component.plan_generate(parsed, context)
            if context.dry_run:
                return component.execute(plan, context)
            if self.reactionopt_adapter is None:
                raise IntegrationNotConfiguredError(
                    "reactionopt.export requires an injected ReactionOptAdapter"
                )
            return component.export(parsed, self.reactionopt_adapter, context)

        if capability_id == "reactionopt.profile":
            component = self._as(provider, ReactionOptPlugin)
            parsed = ReactionProfileRequest.from_mapping(request)
            if context.dry_run:
                return component.execute(component.plan_profile(parsed, context), context)
            if self.reactionopt_profile_adapter is None:
                raise IntegrationNotConfiguredError(
                    "reactionopt.profile requires an injected RitS TS pipeline sampler"
                )
            return component.execute_profile(
                parsed,
                self.reactionopt_profile_adapter,
                context,
            )

        if capability_id == "reaction-db.ingest":
            component = self._as(provider, ReactionDatabasePlugin)
            parsed = ReactionIngestRequest.from_mapping(request)
            repository = self.reaction_repository or InMemoryReactionRepository()
            if not context.dry_run and self.reaction_repository is None:
                raise IntegrationNotConfiguredError(
                    "reaction-db.ingest requires an injected ReactionRepository"
                )
            return component.ingest(parsed, repository, context)

        if capability_id == "reaction-db.batch-import":
            component = self._as(provider, ReactionDatabasePlugin)
            if context.dry_run:
                return component.execute(planned, context)
            if self.reaction_database_adapter is None:
                raise IntegrationNotConfiguredError(
                    "reaction-db.batch-import requires an injected ReactionDatabaseAdapter"
                )
            parsed = ReactionBatchImportRequest.from_mapping(request)
            return component.ingest_batch(parsed, self.reaction_database_adapter, context)

        if capability_id == "reaction-db.filter":
            component = self._as(provider, ReactionDatabasePlugin)
            if context.dry_run:
                return component.execute(planned, context)
            if self.reaction_database_adapter is None:
                raise IntegrationNotConfiguredError(
                    "reaction-db.filter requires an injected ReactionDatabaseAdapter"
                )
            parsed = ReactionFilterRequest.from_mapping(request)
            return component.filter_reactions(parsed, self.reaction_database_adapter, context)

        if capability_id == "reaction-db.export-rits":
            component = self._as(provider, ReactionDatabasePlugin)
            if context.dry_run:
                return component.execute(planned, context)
            if self.reaction_database_adapter is None:
                raise IntegrationNotConfiguredError(
                    "reaction-db.export-rits requires an injected ReactionDatabaseAdapter"
                )
            parsed = RitsDatasetExportRequest.from_mapping(request)
            return component.export_rits(parsed, self.reaction_database_adapter, context)

        if capability_id in {"reaction-db.export-rxnresid", "reaction-db.export-space"}:
            component = self._as(provider, ReactionDatabasePlugin)
            if context.dry_run:
                return component.execute(planned, context)
            if self.reaction_database_adapter is None:
                raise IntegrationNotConfiguredError(
                    f"{capability_id} requires an injected ReactionDatabaseAdapter"
                )
            if capability_id == "reaction-db.export-space":
                parsed = ReactionSpaceExportRequest.from_mapping(request)
                return component.export_space(parsed, self.reaction_database_adapter, context)
            parsed = RxnResidDatasetExportRequest.from_mapping(request)
            return component.export_rxnresid(parsed, self.reaction_database_adapter, context)

        if capability_id in {"sar.train", "sar.infer", "sar.finetune"}:
            component = self._as(provider, SarPlugin)
            plan = binding.plan(request, context)
            if context.dry_run:
                return component.execute(plan, context)
            return component.submit_to_bohr(plan, self._require_bohr(), context)

        if capability_id in {"rits.train", "rits.infer", "rits.finetune"}:
            component = self._as(provider, RitsPlugin)
            plan = binding.plan(request, context)
            if context.dry_run:
                return component.execute(plan, context)
            return component.submit_to_bohr(plan, self._require_bohr(), context)

        if capability_id == "bohr.compute.plan":
            # Planning is intrinsically dry-run even if a caller passes the
            # live flag for a uniform command-line workflow.
            planning_context = context
            if not context.dry_run:
                planning_context = ExecutionContext(
                    run_id=context.run_id,
                    workdir=context.workdir,
                    dry_run=True,
                    metadata=context.metadata,
                )
            return provider.execute(planned, planning_context)

        if capability_id == "bohr.compute.submit":
            component = self._as(provider, BohrComputePlugin)
            return component.submit(planned, self._gateway_for(context), context)

        if capability_id == "bohr.job.status":
            component = self._as(provider, BohrComputePlugin)
            return component.status(planned, self._gateway_for(context), context)

        if capability_id == "bohr.job.download":
            component = self._as(provider, BohrComputePlugin)
            return component.download(planned, self._gateway_for(context), context)

        if capability_id == "bohr.batch.prepare":
            component = self._as(provider, BohrSubmitPlugin)
            return component.prepare(binding.operation, request, context)

        if capability_id == "dpa-berny.prepare":
            component = self._as(provider, BohrSubmitPlugin)
            return component.prepare(binding.operation, request, context)

        if capability_id == "dpa-berny.rescue":
            component = self._as(provider, BohrSubmitPlugin)
            return component.rescue(request, context)

        if capability_id in {
            "bohr.batch.submit",
            "bohr.batch.status",
            "bohr.batch.download",
            "bohr.batch.watch",
            "bohr.batch.terminate",
        }:
            component = self._as(provider, BohrSubmitPlugin)
            run_dir = _run_dir(request)
            gateway = self._gateway_for(context)
            if capability_id == "bohr.batch.submit":
                return component.submit_prepared(
                    run_dir,
                    gateway,
                    context,
                    continue_on_error=bool(request.get("continue_on_error", False)),
                )
            if capability_id == "bohr.batch.status":
                return component.status(run_dir, gateway, context)
            if capability_id == "bohr.batch.download":
                return component.download(
                    run_dir,
                    gateway,
                    context,
                    only_succeeded=bool(request.get("only_succeeded", False)),
                    continue_on_error=bool(request.get("continue_on_error", False)),
                    resume=bool(request.get("resume", False)),
                )
            if capability_id == "bohr.batch.watch":
                return component.watch(
                    run_dir,
                    gateway,
                    context,
                    interval=float(request.get("interval", 60)),
                    max_iterations=int(request.get("max_iterations", 0)),
                    download_when_complete=bool(request.get("download_when_complete", False)),
                )
            return component.terminate(
                run_dir,
                gateway,
                context,
                confirm=confirm,
                all_incomplete=bool(request.get("all_incomplete", False)),
                wanted_results=tuple(
                    str(value) for value in request.get("wanted_results", ("RUNNING", "PENDING"))
                ),
                wanted_workers=tuple(str(value) for value in request.get("wanted_workers", ())),
            )

        raise InvalidRequestError(f"capability has no execution handler: {capability_id}")

    def with_bohr_cli(self, config_path: Path | None = None) -> CapabilityDispatcher:
        if config_path is None:
            gateway = BohrCliGateway()
        else:
            gateway = BohrCliGateway(BohrCliConfig.from_toml(config_path))
        return CapabilityDispatcher(
            self.catalog,
            bohr_gateway=gateway,
            reaction_repository=self.reaction_repository,
            reaction_database_adapter=self.reaction_database_adapter,
            reactionopt_adapter=self.reactionopt_adapter,
            reactionopt_profile_adapter=self.reactionopt_profile_adapter,
        )

    def _require_bohr(self) -> BohrGateway:
        if self.bohr_gateway is None:
            raise IntegrationNotConfiguredError(
                "this capability requires an injected BohrGateway; use --live with bohr credentials"
            )
        return self.bohr_gateway

    def _gateway_for(self, context: ExecutionContext) -> BohrGateway:
        if self.bohr_gateway is not None:
            return self.bohr_gateway
        if context.dry_run:
            return InMemoryBohrGateway()
        return self._require_bohr()

    @staticmethod
    def _as(provider: object, expected_type: type[Any]) -> Any:
        if not isinstance(provider, expected_type):
            raise InvalidRequestError(
                f"capability provider has unexpected type: expected {expected_type.__name__}"
            )
        return provider
