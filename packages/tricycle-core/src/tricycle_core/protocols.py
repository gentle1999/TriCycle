from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from .models import (
    BohrJobSpec,
    ExecutionContext,
    PluginManifest,
    ReactionRecord,
    TaskPlan,
    TaskResult,
)


class Component(Protocol):
    """Minimal component contract used by the capability catalog."""

    @property
    def manifest(self) -> PluginManifest: ...

    def plan(
        self,
        operation: str,
        request: dict[str, Any],
        context: ExecutionContext,
    ) -> TaskPlan: ...

    def execute(self, plan: TaskPlan, context: ExecutionContext) -> TaskResult: ...


class BohrStatusGateway(Protocol):
    def status(self, external_id: str) -> dict[str, Any]: ...


class BohrDownloadGateway(Protocol):
    def download(self, external_id: str, destination: Path) -> tuple[str, ...]: ...


class BohrGateway(BohrStatusGateway, BohrDownloadGateway, Protocol):
    def submit(self, job: BohrJobSpec) -> str: ...


class ReactionRepository(Protocol):
    def upsert(self, record: ReactionRecord) -> str: ...


class ReactionOptAdapter(Protocol):
    def generate(
        self, config: dict[str, Any], destination: Path | None = None
    ) -> tuple[str, ...]: ...


class CandidateScorer(Protocol):
    def score(self, candidate: dict[str, Any]) -> float: ...


class ArtifactStore(Protocol):
    def put(self, source: Path, artifact_id: str) -> str: ...
