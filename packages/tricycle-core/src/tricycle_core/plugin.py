from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .errors import IntegrationNotConfiguredError
from .models import ExecutionContext, PluginManifest, TaskPlan, TaskResult, TaskStatus


class PluginBase(ABC):
    """Small common implementation shared by all domain plugins."""

    @property
    @abstractmethod
    def manifest(self) -> PluginManifest:
        """Return immutable metadata used by the registry and agent."""

    @abstractmethod
    def plan(
        self,
        operation: str,
        request: dict[str, Any],
        context: ExecutionContext,
    ) -> TaskPlan:
        """Build a side-effect-free task plan."""

    def execute(self, plan: TaskPlan, context: ExecutionContext) -> TaskResult:
        """Return a dry-run result; real execution belongs to a plugin adapter."""
        if not context.dry_run:
            raise IntegrationNotConfiguredError(
                f"{self.manifest.plugin_id} has no default execution adapter; inject one explicitly"
            )
        return TaskResult(
            task_id=plan.task_id,
            plugin_id=plan.plugin_id,
            status=TaskStatus.PLANNED,
            message="dry-run: no external side effect was performed",
            metadata={"context": context.to_dict()},
        )
