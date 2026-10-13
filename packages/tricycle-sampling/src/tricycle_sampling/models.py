from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from tricycle_core.errors import InvalidRequestError
from tricycle_core.validation import validate_fields


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise InvalidRequestError("candidate_id must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "payload": dict(self.payload)}


@dataclass(frozen=True, slots=True)
class SamplingRequest:
    candidates: tuple[Candidate, ...]
    budget: int
    strategy: str = "random"
    seed: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.budget < 0:
            raise InvalidRequestError("sampling budget must be non-negative")
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise InvalidRequestError("candidate IDs must be unique")
        if self.budget > len(self.candidates):
            raise InvalidRequestError("sampling budget cannot exceed candidate count")
        if not self.strategy.strip():
            raise InvalidRequestError("sampling strategy must not be empty")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> SamplingRequest:
        validate_fields(payload, (item.name for item in fields(cls)), context=cls.__name__)
        raw_candidates = payload.get("candidates", ())
        if not isinstance(raw_candidates, (list, tuple)):
            raise InvalidRequestError("candidates must be a list")
        candidates = []
        for candidate in raw_candidates:
            if isinstance(candidate, Candidate):
                candidates.append(candidate)
                continue
            if not isinstance(candidate, Mapping):
                raise InvalidRequestError("candidate must be a table")
            validate_fields(candidate, {"candidate_id", "payload"}, context="candidate")
            candidates.append(
                Candidate(
                    candidate_id=str(candidate.get("candidate_id", "")),
                    payload=dict(candidate.get("payload", {})),
                )
            )
        return cls(
            candidates=tuple(candidates),
            budget=int(payload.get("budget", 0)),
            strategy=str(payload.get("strategy", "random")),
            seed=payload.get("seed"),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(frozen=True, slots=True)
class SamplingResult:
    strategy: str
    selected: tuple[Candidate, ...]
    remaining: tuple[Candidate, ...]
    seed: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(candidate.candidate_id for candidate in self.selected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "selected": [candidate.to_dict() for candidate in self.selected],
            "remaining": [candidate.to_dict() for candidate in self.remaining],
            "seed": self.seed,
            "metadata": dict(self.metadata),
        }
