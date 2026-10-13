from __future__ import annotations

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
from tricycle_core.protocols import ReactionRepository

from .models import (
    ReactionBatchImportRequest,
    ReactionFilterRequest,
    ReactionIngestRequest,
    RitsDatasetExportRequest,
    RxnResidDatasetExportRequest,
)
from .repository import (
    ReactionDatabaseAdapter,
    ReactionDatabaseImportReceipt,
    ReactionFilterReceipt,
    ReactionSpaceExportReceipt,
    validate_export_receipt,
)


class ReactionDatabasePlugin(PluginBase):
    _manifest = PluginManifest(
        plugin_id="tricycle.reaction-database",
        display_name="Reaction database ingest",
        version="0.1.0",
        kind=PluginKind.REACTION_DATABASE,
        description=(
            "Validate project-scoped calculation-log imports and export cleaned "
            "reaction space through the database's authoritative rxn-SMARTS "
            "predicate and an injected adapter."
        ),
        capabilities=(
            "reaction.validate",
            "reaction.ingest",
            "reaction.upsert",
            "reaction.batch.validate",
            "reaction.batch.ingest",
            "reaction.filter.cycloaddition",
            "reaction.space.rits.export",
            "reaction.space.rxnresid.export",
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
        if operation == "ingest":
            return self.plan_ingest(ReactionIngestRequest.from_mapping(request), context)
        if operation == "batch-import":
            return self.plan_batch_import(ReactionBatchImportRequest.from_mapping(request), context)
        if operation == "filter":
            return self.plan_filter(ReactionFilterRequest.from_mapping(request), context)
        if operation == "export-rits":
            return self.plan_rits_export(RitsDatasetExportRequest.from_mapping(request), context)
        if operation == "export-rxnresid":
            return self.plan_rxnresid_export(
                RxnResidDatasetExportRequest.from_mapping(request), context
            )
        raise InvalidRequestError(f"unsupported reaction database operation: {operation}")

    def plan_ingest(self, request: ReactionIngestRequest, context: ExecutionContext) -> TaskPlan:
        return TaskPlan(
            task_id=f"{context.run_id}/reaction-db/{request.record.reaction_id}",
            plugin_id=self.manifest.plugin_id,
            operation="ingest",
            inputs=request.record.artifacts,
            parameters={
                "record": request.record.to_dict(),
                "idempotency_key": request.idempotency_key,
                "validate_only": request.validate_only,
                "source": request.source,
                "metadata": dict(request.metadata),
            },
            metadata={"reaction_id": request.record.reaction_id, "dry_run": context.dry_run},
        )

    def plan_batch_import(
        self,
        request: ReactionBatchImportRequest,
        context: ExecutionContext,
    ) -> TaskPlan:
        manifest = request.resolve_batch(context.workdir)
        input_artifact = Artifact(
            artifact_id=f"reaction-db-batch-{manifest.batch_id}",
            location=str(manifest.source_dir),
            kind="directory",
            metadata={
                "batch_id": manifest.batch_id,
                "included_suffix": ".log",
                "log_count": len(manifest.files),
                "total_bytes": manifest.total_bytes,
                "manifest_digest": manifest.manifest_digest,
            },
        )
        return TaskPlan(
            task_id=f"{context.run_id}/reaction-db/batch/{request.project.slug}",
            plugin_id=self.manifest.plugin_id,
            operation="batch-import",
            inputs=(input_artifact,),
            parameters={
                "project": request.project.to_dict(),
                "source": request.source,
                "provenance": dict(request.provenance),
                "idempotency_key": request.resolved_idempotency_key(manifest),
                "batch": manifest.to_dict(),
                "metadata": dict(request.metadata),
            },
            metadata={
                "project_slug": request.project.slug,
                "batch_id": manifest.batch_id,
                "log_count": len(manifest.files),
                "total_bytes": manifest.total_bytes,
                "scope": "one-new-database-project-and-only-this-batch-log-files",
                "dry_run": context.dry_run,
            },
        )

    def plan_filter(
        self,
        request: ReactionFilterRequest,
        context: ExecutionContext,
    ) -> TaskPlan:
        return TaskPlan(
            task_id=f"{context.run_id}/reaction-db/filter/{request.project_id}",
            plugin_id=self.manifest.plugin_id,
            operation="filter",
            parameters={"request": request.to_dict()},
            metadata={
                "project_id": request.project_id,
                "cleaned_only": request.cleaned_only,
                "reaction_smarts": request.reaction_smarts,
                "authority": "database-rxn-smarts",
                "dry_run": context.dry_run,
            },
        )

    def plan_rits_export(
        self,
        request: RitsDatasetExportRequest,
        context: ExecutionContext,
    ) -> TaskPlan:
        return self._plan_export(request, context, operation="export-rits")

    def plan_rxnresid_export(
        self,
        request: RxnResidDatasetExportRequest,
        context: ExecutionContext,
    ) -> TaskPlan:
        return self._plan_export(request, context, operation="export-rxnresid")

    def _plan_export(
        self,
        request: RitsDatasetExportRequest | RxnResidDatasetExportRequest,
        context: ExecutionContext,
        *,
        operation: str,
    ) -> TaskPlan:
        return TaskPlan(
            task_id=f"{context.run_id}/reaction-db/{operation}/{request.project_id}",
            plugin_id=self.manifest.plugin_id,
            operation=operation,
            parameters={"request": request.to_dict()},
            metadata={
                "project_id": request.project_id,
                "consumer": request.consumer,
                "cleaned_only": request.cleaned_only,
                "reaction_smarts": request.reaction_smarts,
                "required_columns": request.to_dict()["required_columns"],
                "authority": "database-rxn-smarts",
                "dry_run": context.dry_run,
            },
        )

    def ingest_batch(
        self,
        request: ReactionBatchImportRequest,
        adapter: ReactionDatabaseAdapter,
        context: ExecutionContext,
    ) -> TaskResult:
        plan = self.plan_batch_import(request, context)
        if context.dry_run:
            return self.execute(plan, context)
        manifest = request.resolve_batch(context.workdir)
        receipt = adapter.ingest_calculation_logs(request, manifest)
        return self._import_result(plan, receipt)

    def filter_reactions(
        self,
        request: ReactionFilterRequest,
        adapter: ReactionDatabaseAdapter,
        context: ExecutionContext,
    ) -> TaskResult:
        plan = self.plan_filter(request, context)
        if context.dry_run:
            return self.execute(plan, context)
        receipt = adapter.filter_cleaned_reactions(request)
        return self._filter_result(plan, receipt)

    def export_rits(
        self,
        request: RitsDatasetExportRequest,
        adapter: ReactionDatabaseAdapter,
        context: ExecutionContext,
    ) -> TaskResult:
        plan = self.plan_rits_export(request, context)
        if context.dry_run:
            return self.execute(plan, context)
        return self._export_result(plan, request, adapter.export_rits_dataset(request))

    def export_rxnresid(
        self,
        request: RxnResidDatasetExportRequest,
        adapter: ReactionDatabaseAdapter,
        context: ExecutionContext,
    ) -> TaskResult:
        plan = self.plan_rxnresid_export(request, context)
        if context.dry_run:
            return self.execute(plan, context)
        return self._export_result(plan, request, adapter.export_rxnresid_dataset(request))

    @staticmethod
    def _filter_result(plan: TaskPlan, receipt: ReactionFilterReceipt) -> TaskResult:
        return TaskResult(
            task_id=plan.task_id,
            plugin_id=plan.plugin_id,
            status=TaskStatus.SUCCEEDED,
            external_id=receipt.project_id,
            message="database-cleaned reactions selected by rxn-SMARTS",
            metadata={**receipt.to_dict(), "authority": "database-rxn-smarts"},
        )

    @classmethod
    def _export_result(
        cls,
        plan: TaskPlan,
        request: RitsDatasetExportRequest | RxnResidDatasetExportRequest,
        receipt: ReactionSpaceExportReceipt,
    ) -> TaskResult:
        validate_export_receipt(request, receipt)
        status = cls._status(receipt.status)
        output = Artifact(
            artifact_id=f"{request.consumer.casefold()}-{request.project_id}-dataset",
            location=str(receipt.destination),
            kind="file",
            media_type="text/csv",
            metadata={
                "consumer": request.consumer,
                "cleaned_only": request.cleaned_only,
                "reaction_smarts": request.reaction_smarts,
                "required_columns": request.to_dict()["required_columns"],
                "row_count": receipt.row_count,
                **dict(receipt.metadata),
            },
        )
        return TaskResult(
            task_id=plan.task_id,
            plugin_id=plan.plugin_id,
            status=status,
            outputs=(output,) if status is TaskStatus.SUCCEEDED else (),
            message="database-cleaned reaction space exported" if status is TaskStatus.SUCCEEDED else receipt.status,
            metadata={"project_id": request.project_id, **receipt.to_dict()},
        )

    @staticmethod
    def _status(value: str) -> TaskStatus:
        normalized = value.casefold().strip()
        if normalized in {"failed", "failure", "error"}:
            return TaskStatus.FAILED
        if normalized in {"planned", "pending", "queued"}:
            return TaskStatus.PLANNED
        if normalized in {"running", "processing", "in-progress", "in_progress"}:
            return TaskStatus.RUNNING
        if normalized in {"submitted", "accepted"}:
            return TaskStatus.SUBMITTED
        return TaskStatus.SUCCEEDED

    @classmethod
    def _import_result(
        cls,
        plan: TaskPlan,
        receipt: ReactionDatabaseImportReceipt,
    ) -> TaskResult:
        status = cls._status(receipt.status)
        return TaskResult(
            task_id=plan.task_id,
            plugin_id=plan.plugin_id,
            status=status,
            external_id=receipt.project_id,
            message="calculation-log batch imported" if status is TaskStatus.SUCCEEDED else receipt.status,
            metadata={**receipt.to_dict(), "scope": plan.metadata["scope"]},
        )

    def ingest(
        self,
        request: ReactionIngestRequest,
        repository: ReactionRepository,
        context: ExecutionContext,
    ) -> TaskResult:
        plan = self.plan_ingest(request, context)
        if request.validate_only:
            return TaskResult(
                task_id=plan.task_id,
                plugin_id=self.manifest.plugin_id,
                status=TaskStatus.PLANNED,
                message="validation-only: no database side effect was performed",
                metadata={"reaction_id": request.record.reaction_id},
            )
        if context.dry_run:
            return self.execute(plan, context)
        record_id = repository.upsert(request.record)
        return TaskResult(
            task_id=plan.task_id,
            plugin_id=self.manifest.plugin_id,
            status=TaskStatus.SUCCEEDED,
            external_id=record_id,
            message="reaction record upserted",
            metadata={"reaction_id": request.record.reaction_id},
        )
