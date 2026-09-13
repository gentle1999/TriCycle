from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import InvalidRequestError

if TYPE_CHECKING:
    from .trace import TraceSink


class PluginKind(StrEnum):
    BOHR_COMPUTE = "bohr-compute"
    BOHR_WORKFLOW = "bohr-workflow"
    REACTION_DATABASE = "reaction-database"
    SAR = "sar"
    RITS = "rits"
    SAMPLING = "sampling"
    REACTIONOPT = "reactionopt"
    EXPLORATION = "exploration"


class TaskStatus(StrEnum):
    PLANNED = "planned"
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ModelMode(StrEnum):
    TRAIN = "train"
    INFER = "infer"
    FINETUNE = "finetune"
    FINE_TUNE = "finetune"


@dataclass(frozen=True, slots=True)
class PluginManifest:
    plugin_id: str
    display_name: str
    version: str
    kind: PluginKind
    description: str
    capabilities: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.plugin_id.strip():
            raise InvalidRequestError("plugin_id must not be empty")
        if not self.version.strip():
            raise InvalidRequestError("plugin version must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "display_name": self.display_name,
            "version": self.version,
            "kind": self.kind.value,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "dependencies": list(self.dependencies),
        }


@dataclass(frozen=True, slots=True)
class Artifact:
    artifact_id: str
    location: str
    kind: str = "file"
    media_type: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise InvalidRequestError("artifact_id must not be empty")
        if not self.location.strip():
            raise InvalidRequestError("artifact location must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "location": self.location,
            "kind": self.kind,
            "media_type": self.media_type,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    run_id: str
    workdir: Path = Path(".")
    dry_run: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)
    trace_sink: TraceSink | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise InvalidRequestError("run_id must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workdir": str(self.workdir),
            "dry_run": self.dry_run,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ReactionRecord:
    reaction_id: str
    reaction_smiles: str
    reactants: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    conditions: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)
    artifacts: tuple[Artifact, ...] = ()

    def __post_init__(self) -> None:
        if not self.reaction_id.strip():
            raise InvalidRequestError("reaction_id must not be empty")
        if not self.reaction_smiles.strip():
            raise InvalidRequestError("reaction_smiles must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "reaction_id": self.reaction_id,
            "reaction_smiles": self.reaction_smiles,
            "reactants": list(self.reactants),
            "products": list(self.products),
            "conditions": dict(self.conditions),
            "provenance": dict(self.provenance),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }


@dataclass(frozen=True, slots=True)
class BohrJobSpec:
    name: str
    operation: str
    image: str
    command: tuple[str, ...] = ()
    project_id: str | None = None
    machine_type: str | None = None
    input_artifacts: tuple[Artifact, ...] = ()
    output_artifacts: tuple[Artifact, ...] = ()
    parameters: Mapping[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("name", "operation", "image"):
            if not getattr(self, field_name).strip():
                raise InvalidRequestError(f"{field_name} must not be empty")
        if any(not part.strip() for part in self.command):
            raise InvalidRequestError("command entries must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "operation": self.operation,
            "image": self.image,
            "command": list(self.command),
            "project_id": self.project_id,
            "machine_type": self.machine_type,
            "input_artifacts": [artifact.to_dict() for artifact in self.input_artifacts],
            "output_artifacts": [artifact.to_dict() for artifact in self.output_artifacts],
            "parameters": dict(self.parameters),
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class TaskPlan:
    task_id: str
    plugin_id: str
    operation: str
    inputs: tuple[Artifact, ...] = ()
    parameters: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "plugin_id": self.plugin_id,
            "operation": self.operation,
            "inputs": [artifact.to_dict() for artifact in self.inputs],
            "parameters": dict(self.parameters),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TaskResult:
    task_id: str
    plugin_id: str
    status: TaskStatus
    outputs: tuple[Artifact, ...] = ()
    external_id: str | None = None
    message: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "plugin_id": self.plugin_id,
            "status": self.status.value,
            "outputs": [artifact.to_dict() for artifact in self.outputs],
            "external_id": self.external_id,
            "message": self.message,
            "metadata": dict(self.metadata),
        }
