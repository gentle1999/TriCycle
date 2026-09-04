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
from tricycle_core.protocols import ReactionOptAdapter

from .models import ReactionConfig, ReactionConfigRequest


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
