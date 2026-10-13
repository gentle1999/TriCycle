from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from tricycle_core import InvalidRequestError, sha256_value
from tricycle_core.validation import validate_fields
from tricycle_sampling import Candidate


def _mapping(value: object, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{field_name} must be a table")
    return dict(value)


def _strings(value: object, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise InvalidRequestError(f"{field_name} must be a list")
    result = tuple(str(item) for item in value)
    if any(not item.strip() for item in result):
        raise InvalidRequestError(f"{field_name} must not contain empty values")
    return result


def _integer(value: object, *, field_name: str, default: int) -> int:
    try:
        if value is not None and not isinstance(value, (str, int, float)):
            raise TypeError("expected a numeric value")
        return int(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError(f"{field_name} must be an integer") from exc


def _boolean(value: object, *, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise InvalidRequestError(f"{field_name} must be a boolean")


@dataclass(frozen=True, slots=True)
class SpaceDefinition:
    """Versioned definition of the space from which a candidate pool came."""

    space_id: str
    version: str = "1.0.0"
    definition: Mapping[str, Any] = field(default_factory=dict)
    source_artifacts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.space_id.strip():
            raise InvalidRequestError("space_id must not be empty")
        if not self.version.strip():
            raise InvalidRequestError("space version must not be empty")

    @classmethod
    def from_mapping(cls, value: object, *, default_id: str) -> SpaceDefinition:
        raw = _mapping(value, field_name="space")
        validate_fields(raw, (item.name for item in fields(cls)), context=cls.__name__)
        space_id = str(raw.get("space_id", default_id))
        version = str(raw.get("version", "1.0.0"))
        definition_value = raw.get("definition")
        definition = _mapping(definition_value, field_name="space.definition")
        return cls(
            space_id=space_id,
            version=version,
            definition=definition,
            source_artifacts=_strings(
                raw.get("source_artifacts"), field_name="space.source_artifacts"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "space_id": self.space_id,
            "version": self.version,
            "definition": dict(self.definition),
            "source_artifacts": list(self.source_artifacts),
        }

    @property
    def fingerprint(self) -> str:
        return sha256_value(self.to_dict())


@dataclass(frozen=True, slots=True)
class ModelSnapshot:
    """Immutable model identity used to produce scores for a round."""

    snapshot_id: str
    checkpoint_hash: str = ""
    score_artifact: str = ""
    source_run_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.snapshot_id.strip():
            raise InvalidRequestError("model_snapshot.snapshot_id must not be empty")

    @classmethod
    def from_mapping(cls, value: object) -> ModelSnapshot:
        raw = _mapping(value, field_name="model_snapshot")
        validate_fields(raw, (item.name for item in fields(cls)), context=cls.__name__)
        return cls(
            snapshot_id=str(raw.get("snapshot_id", "unspecified")),
            checkpoint_hash=str(raw.get("checkpoint_hash", "")),
            score_artifact=str(raw.get("score_artifact", "")),
            source_run_id=str(raw.get("source_run_id", "")),
            metadata=_mapping(raw.get("metadata"), field_name="model_snapshot.metadata"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "checkpoint_hash": self.checkpoint_hash,
            "score_artifact": self.score_artifact,
            "source_run_id": self.source_run_id,
            "metadata": dict(self.metadata),
        }

    @property
    def fingerprint(self) -> str:
        return sha256_value(self.to_dict())


@dataclass(frozen=True, slots=True)
class SnapshotTransition:
    """Replay contract connecting the snapshot used by a round to its successor."""

    source_snapshot: ModelSnapshot
    action: str
    status: str
    next_snapshot: ModelSnapshot
    replay_key: str
    round_decision_hash: str
    dataset_before_hash: str = ""
    dataset_after_hash: str = ""
    training_spec: Mapping[str, Any] = field(default_factory=dict)
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.action not in {"skip", "train", "finetune"}:
            raise InvalidRequestError("snapshot transition action must be skip, train, or finetune")
        if self.status not in {"carried_forward", "planned", "realized"}:
            raise InvalidRequestError(
                "snapshot transition status must be carried_forward, planned, or realized"
            )
        if not self.replay_key.strip() or not self.round_decision_hash.strip():
            raise InvalidRequestError("snapshot transition hashes must not be empty")
        if self.action == "skip" and self.status != "carried_forward":
            raise InvalidRequestError("a skipped training decision must carry the snapshot forward")
        if self.action in {"train", "finetune"} and self.status == "carried_forward":
            raise InvalidRequestError("a training decision cannot carry the snapshot forward")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "source_snapshot": self.source_snapshot.to_dict(),
            "source_snapshot_hash": self.source_snapshot.fingerprint,
            "action": self.action,
            "status": self.status,
            "next_snapshot": self.next_snapshot.to_dict(),
            "next_snapshot_hash": self.next_snapshot.fingerprint,
            "replay_key": self.replay_key,
            "round_decision_hash": self.round_decision_hash,
            "dataset_before_hash": self.dataset_before_hash,
            "dataset_after_hash": self.dataset_after_hash,
            "training_spec": dict(self.training_spec),
            "rationale": self.rationale,
        }

    @property
    def transition_hash(self) -> str:
        return sha256_value(self._base_dict())

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "transition_hash": self.transition_hash}


@dataclass(frozen=True, slots=True)
class RangePolicyConfig:
    policy_id: str = "fixed-space"
    version: str = "1.0.0"
    mode: str = "fixed"
    parameters: Mapping[str, Any] = field(default_factory=dict)
    rationale: str = ""

    def __post_init__(self) -> None:
        for field_name in ("policy_id", "version", "mode"):
            if not getattr(self, field_name).strip():
                raise InvalidRequestError(f"range_policy.{field_name} must not be empty")

    @classmethod
    def from_mapping(cls, value: object) -> RangePolicyConfig:
        raw = _mapping(value, field_name="range_policy")
        validate_fields(raw, (item.name for item in fields(cls)), context=cls.__name__)
        return cls(
            policy_id=str(raw.get("policy_id", "fixed-space")),
            version=str(raw.get("version", "1.0.0")),
            mode=str(raw.get("mode", "fixed")),
            parameters=_mapping(raw.get("parameters"), field_name="range_policy.parameters"),
            rationale=str(raw.get("rationale", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "mode": self.mode,
            "parameters": dict(self.parameters),
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class RangeDecision:
    policy_id: str
    policy_version: str
    method: str
    input_space: SpaceDefinition
    next_space: SpaceDefinition
    parameters: Mapping[str, Any] = field(default_factory=dict)
    observed_metrics: Mapping[str, Any] = field(default_factory=dict)
    rationale: str = ""

    @property
    def decision_hash(self) -> str:
        return sha256_value(self._base_dict())

    def _base_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "method": self.method,
            "input_space": self.input_space.to_dict(),
            "next_space": self.next_space.to_dict(),
            "parameters": dict(self.parameters),
            "observed_metrics": dict(self.observed_metrics),
            "rationale": self.rationale,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "decision_hash": self.decision_hash}


@dataclass(frozen=True, slots=True)
class TrainingPolicyConfig:
    policy_id: str = "rits-training-gate"
    version: str = "1.0.0"
    mode: str = "never"
    interval: int = 0
    min_new_labels: int = 0
    train_mode: str = "auto"
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("policy_id", "version", "mode", "train_mode"):
            if not getattr(self, field_name).strip():
                raise InvalidRequestError(f"training_policy.{field_name} must not be empty")
        if self.interval < 0 or self.min_new_labels < 0:
            raise InvalidRequestError("training policy thresholds must not be negative")

    @classmethod
    def from_mapping(cls, value: object) -> TrainingPolicyConfig:
        raw = _mapping(value, field_name="training_policy")
        validate_fields(raw, (item.name for item in fields(cls)), context=cls.__name__)
        return cls(
            policy_id=str(raw.get("policy_id", "rits-training-gate")),
            version=str(raw.get("version", "1.0.0")),
            mode=str(raw.get("mode", "never")),
            interval=_integer(
                raw.get("interval"), field_name="training_policy.interval", default=0
            ),
            min_new_labels=_integer(
                raw.get("min_new_labels"), field_name="training_policy.min_new_labels", default=0
            ),
            train_mode=str(raw.get("train_mode", "auto")),
            parameters=_mapping(raw.get("parameters"), field_name="training_policy.parameters"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "mode": self.mode,
            "interval": self.interval,
            "min_new_labels": self.min_new_labels,
            "train_mode": self.train_mode,
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True, slots=True)
class TrainingDecision:
    policy_id: str
    policy_version: str
    action: str
    reason_code: str
    next_capability: str
    requested_mode: str
    previous_checkpoint: str
    evaluated_inputs: Mapping[str, Any] = field(default_factory=dict)
    policy_parameters: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def decision_hash(self) -> str:
        return sha256_value(self._base_dict())

    def _base_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "action": self.action,
            "reason_code": self.reason_code,
            "next_capability": self.next_capability,
            "requested_mode": self.requested_mode,
            "previous_checkpoint": self.previous_checkpoint,
            "evaluated_inputs": dict(self.evaluated_inputs),
            "policy_parameters": dict(self.policy_parameters),
            "reason": self.reason,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "decision_hash": self.decision_hash}


@dataclass(frozen=True, slots=True)
class ExplorationRoundRequest:
    exploration_id: str
    round_index: int
    candidates: tuple[Candidate, ...]
    budget: int
    strategy: str = "random"
    seed: int | None = None
    space: SpaceDefinition = field(default_factory=lambda: SpaceDefinition("unspecified-space"))
    parent_round_id: str = ""
    next_space: SpaceDefinition | None = None
    range_policy: RangePolicyConfig = field(default_factory=RangePolicyConfig)
    model_snapshot: ModelSnapshot = field(default_factory=lambda: ModelSnapshot("unspecified"))
    next_model_snapshot: ModelSnapshot | None = None
    training_policy: TrainingPolicyConfig = field(default_factory=TrainingPolicyConfig)
    last_training_round: int = -1
    new_label_count: int = 0
    dataset_before_hash: str = ""
    dataset_after_hash: str = ""
    previous_checkpoint: str = ""
    training_action: str = ""
    training_spec: Mapping[str, Any] = field(default_factory=dict)
    observed_metrics: Mapping[str, Any] = field(default_factory=dict)
    excluded_candidates: Mapping[str, str] = field(default_factory=dict)
    seen_candidate_ids: tuple[str, ...] = ()
    exclude_seen: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    output_dir: Path | None = None
    resume: bool = False

    def __post_init__(self) -> None:
        if not self.exploration_id.strip():
            raise InvalidRequestError("exploration_id must not be empty")
        if self.round_index < 1:
            raise InvalidRequestError("round_index must be positive")
        if self.budget < 0 or self.budget > len(self.candidates):
            raise InvalidRequestError("sampling budget must be within the candidate pool")
        if not self.strategy.strip():
            raise InvalidRequestError("sampling strategy must not be empty")
        if self.new_label_count < 0 or self.last_training_round < -1:
            raise InvalidRequestError("training round counters are invalid")
        if len({candidate.candidate_id for candidate in self.candidates}) != len(self.candidates):
            raise InvalidRequestError("candidate IDs must be unique")

    @property
    def round_id(self) -> str:
        return f"{self.exploration_id}/round-{self.round_index:04d}"

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ExplorationRoundRequest:
        validate_fields(
            payload,
            {
                "exploration_id",
                "round_index",
                "candidates",
                "budget",
                "strategy",
                "seed",
                "space",
                "parent_round_id",
                "next_space",
                "range_policy",
                "model_snapshot",
                "next_model_snapshot",
                "training_policy",
                "training_context",
                "training_spec",
                "observed_metrics",
                "excluded_candidates",
                "history",
                "metadata",
                "output_dir",
                "resume",
            },
            context="exploration round",
        )
        exploration_id = str(payload.get("exploration_id", ""))
        round_index = _integer(payload.get("round_index"), field_name="round_index", default=0)
        raw_candidates = payload.get("candidates", ())
        if not isinstance(raw_candidates, (list, tuple)):
            raise InvalidRequestError("candidates must be a list")
        candidates: list[Candidate] = []
        for index, raw_candidate in enumerate(raw_candidates):
            if isinstance(raw_candidate, Candidate):
                candidates.append(raw_candidate)
                continue
            if not isinstance(raw_candidate, Mapping):
                raise InvalidRequestError(f"candidates[{index}] must be a table")
            validate_fields(raw_candidate, {"candidate_id", "payload"}, context="candidate")
            candidate_id = str(raw_candidate.get("candidate_id", ""))
            candidate_payload = _mapping(
                raw_candidate.get("payload"), field_name="candidate.payload"
            )
            candidates.append(Candidate(candidate_id, candidate_payload))

        raw_excluded = payload.get("excluded_candidates", ())
        if not isinstance(raw_excluded, (list, tuple)):
            raise InvalidRequestError("excluded_candidates must be a list")
        excluded: dict[str, str] = {}
        for index, item in enumerate(raw_excluded):
            if isinstance(item, Mapping):
                validate_fields(item, {"candidate_id", "reason"}, context="excluded candidate")
                candidate_id = str(item.get("candidate_id", ""))
                reason = str(item.get("reason", "excluded_by_request"))
            else:
                candidate_id = str(item)
                reason = "excluded_by_request"
            if not candidate_id.strip():
                raise InvalidRequestError(f"excluded_candidates[{index}] has no candidate ID")
            excluded[candidate_id] = reason

        history = _mapping(payload.get("history"), field_name="history")
        validate_fields(history, {"seen_candidate_ids", "exclude_seen"}, context="history")
        seen = _strings(
            history.get("seen_candidate_ids"),
            field_name="history.seen_candidate_ids",
        )
        exclude_seen = _boolean(
            history.get("exclude_seen"),
            field_name="history.exclude_seen",
            default=bool(seen),
        )
        training_context = _mapping(payload.get("training_context"), field_name="training_context")
        validate_fields(
            training_context,
            {
                "last_training_round",
                "new_label_count",
                "dataset_before_hash",
                "dataset_after_hash",
                "previous_checkpoint",
                "action",
            },
            context="training_context",
        )
        output_value = payload.get("output_dir", None)
        output_dir = Path(str(output_value)) if output_value not in (None, "") else None
        model_snapshot = ModelSnapshot.from_mapping(payload.get("model_snapshot"))
        raw_next_snapshot = payload.get("next_model_snapshot", None)
        next_model_snapshot = (
            ModelSnapshot.from_mapping(raw_next_snapshot) if raw_next_snapshot is not None else None
        )
        training_policy = TrainingPolicyConfig.from_mapping(payload.get("training_policy"))
        training_spec = _mapping(
            payload.get("training_spec"),
            field_name="training_spec",
        )
        return cls(
            exploration_id=exploration_id,
            round_index=round_index,
            candidates=tuple(candidates),
            budget=_integer(payload.get("budget"), field_name="budget", default=0),
            strategy=str(payload.get("strategy", "random")),
            seed=payload.get("seed"),
            space=SpaceDefinition.from_mapping(
                payload.get("space"), default_id=f"{exploration_id}/space/{round_index}"
            ),
            parent_round_id=str(payload.get("parent_round_id", "")),
            next_space=(
                SpaceDefinition.from_mapping(
                    payload.get("next_space"),
                    default_id=f"{exploration_id}/space/{round_index + 1}",
                )
                if payload.get("next_space") is not None
                else None
            ),
            range_policy=RangePolicyConfig.from_mapping(payload.get("range_policy")),
            model_snapshot=model_snapshot,
            next_model_snapshot=next_model_snapshot,
            training_policy=training_policy,
            last_training_round=_integer(
                training_context.get("last_training_round"),
                field_name="training_context.last_training_round",
                default=-1,
            ),
            new_label_count=_integer(
                training_context.get("new_label_count"),
                field_name="training_context.new_label_count",
                default=0,
            ),
            dataset_before_hash=str(training_context.get("dataset_before_hash", "")),
            dataset_after_hash=str(training_context.get("dataset_after_hash", "")),
            previous_checkpoint=str(training_context.get("previous_checkpoint", "")),
            training_action=str(training_context.get("action", "")),
            training_spec=training_spec,
            observed_metrics=_mapping(
                payload.get("observed_metrics"), field_name="observed_metrics"
            ),
            excluded_candidates=excluded,
            seen_candidate_ids=seen,
            exclude_seen=exclude_seen,
            metadata=_mapping(payload.get("metadata"), field_name="metadata"),
            output_dir=output_dir,
            resume=_boolean(payload.get("resume"), field_name="resume", default=False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "exploration_id": self.exploration_id,
            "round_index": self.round_index,
            "parent_round_id": self.parent_round_id,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "budget": self.budget,
            "strategy": self.strategy,
            "seed": self.seed,
            "space": self.space.to_dict(),
            "next_space": self.next_space.to_dict() if self.next_space else None,
            "range_policy": self.range_policy.to_dict(),
            "model_snapshot": self.model_snapshot.to_dict(),
            "next_model_snapshot": (
                self.next_model_snapshot.to_dict() if self.next_model_snapshot else None
            ),
            "training_policy": self.training_policy.to_dict(),
            "training_context": {
                "last_training_round": self.last_training_round,
                "new_label_count": self.new_label_count,
                "dataset_before_hash": self.dataset_before_hash,
                "dataset_after_hash": self.dataset_after_hash,
                "previous_checkpoint": self.previous_checkpoint,
                "action": self.training_action,
            },
            "training_spec": dict(self.training_spec),
            "observed_metrics": dict(self.observed_metrics),
            "excluded_candidates": [
                {"candidate_id": candidate_id, "reason": reason}
                for candidate_id, reason in self.excluded_candidates.items()
            ],
            "history": {
                "seen_candidate_ids": list(self.seen_candidate_ids),
                "exclude_seen": self.exclude_seen,
            },
            "metadata": dict(self.metadata),
            "output_dir": str(self.output_dir) if self.output_dir else "",
            "resume": self.resume,
        }

    @property
    def request_hash(self) -> str:
        # ``resume`` and ``output_dir`` control execution, while
        # ``next_model_snapshot`` is a derived output supplied after RitS
        # finishes. Keeping them out of the round identity lets the same
        # decision replay key bind a later realized checkpoint to an initially
        # planned transition, independent of the local run directory.
        request = self.to_dict()
        request["resume"] = False
        request["output_dir"] = ""
        request["next_model_snapshot"] = None
        return sha256_value(request)
