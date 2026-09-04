from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tricycle_core.errors import InvalidRequestError


@dataclass(frozen=True, slots=True)
class ReactionConfigRequest:
    reaction_id: str
    reaction_smiles: str
    method: str = "4+2"
    output_dir: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.reaction_id.strip():
            raise InvalidRequestError("reaction_id must not be empty")
        if not self.reaction_smiles.strip():
            raise InvalidRequestError("reaction_smiles must not be empty")
        if not self.method.strip():
            raise InvalidRequestError("ReactionOpt method must not be empty")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ReactionConfigRequest:
        return cls(
            reaction_id=str(payload.get("reaction_id", "")),
            reaction_smiles=str(payload.get("reaction_smiles", "")),
            method=str(payload.get("method", "4+2")),
            output_dir=payload.get("output_dir"),
            parameters=dict(payload.get("parameters", {})),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(frozen=True, slots=True)
class ReactionConfig:
    schema_version: str
    reaction_id: str
    reaction_smiles: str
    method: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "reaction_id": self.reaction_id,
            "reaction_smiles": self.reaction_smiles,
            "method": self.method,
            "parameters": dict(self.parameters),
            "metadata": dict(self.metadata),
        }
