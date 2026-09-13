from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tricycle_core import InvalidRequestError, sha256_value

from .models import (
    ModelSnapshot,
    RangeDecision,
    RangePolicyConfig,
    SnapshotTransition,
    SpaceDefinition,
    TrainingDecision,
    TrainingPolicyConfig,
)

_REPLAY_REQUIRED_TRAINING_FIELDS = (
    "seed",
    "dataset_manifest_hash",
    "code_revision",
    "container_digest",
)


def decide_range(
    policy: RangePolicyConfig,
    current_space: SpaceDefinition,
    next_space: SpaceDefinition | None,
    observed_metrics: Mapping[str, Any],
) -> RangeDecision:
    """Resolve a range policy without guessing domain-specific chemistry rules."""

    mode = policy.mode.strip().lower()
    if mode in {"fixed", "inherit"}:
        resolved = current_space
        rationale = policy.rationale or "the policy keeps the current search space"
    elif mode in {"explicit", "manual"}:
        if next_space is None:
            raise InvalidRequestError(
                "range_policy mode explicit/manual requires next_space"
            )
        resolved = next_space
        rationale = policy.rationale or "the next search space was explicitly supplied"
    else:
        raise InvalidRequestError(
            f"unsupported range_policy mode: {policy.mode}; use fixed or explicit"
        )
    return RangeDecision(
        policy_id=policy.policy_id,
        policy_version=policy.version,
        method=mode,
        input_space=current_space,
        next_space=resolved,
        parameters=policy.parameters,
        observed_metrics=observed_metrics,
        rationale=rationale,
    )


def decide_training(
    policy: TrainingPolicyConfig,
    *,
    round_index: int,
    last_training_round: int,
    new_label_count: int,
    dataset_before_hash: str,
    dataset_after_hash: str,
    previous_checkpoint: str,
    manual_action: str = "",
) -> TrainingDecision:
    """Return a deterministic RitS train/skip decision with its evidence."""

    if round_index < 1:
        raise InvalidRequestError("round_index must be positive")
    if last_training_round < -1 or new_label_count < 0:
        raise InvalidRequestError("training decision inputs are invalid")

    mode = policy.mode.strip().lower()
    inputs = {
        "round_index": round_index,
        "last_training_round": last_training_round,
        "new_label_count": new_label_count,
        "dataset_before_hash": dataset_before_hash,
        "dataset_after_hash": dataset_after_hash,
        "previous_checkpoint": previous_checkpoint,
        "manual_action": manual_action,
    }
    due = False
    reason_code = ""
    reason = ""
    if mode == "never":
        reason_code = "policy_never"
        reason = "the configured policy never schedules RitS training"
    elif mode == "always":
        due = True
        reason_code = "policy_always"
        reason = "the configured policy schedules training every round"
    elif mode == "every_n_rounds":
        if policy.interval < 1:
            raise InvalidRequestError("training_policy.interval must be positive for every_n_rounds")
        due = last_training_round < 0 or round_index - last_training_round >= policy.interval
        reason_code = "interval_reached" if due else "interval_not_reached"
        reason = (
            f"{round_index - last_training_round} round(s) since the last training"
            if last_training_round >= 0
            else "no previous RitS training is recorded"
        )
    elif mode == "new_labels":
        if policy.min_new_labels < 1:
            raise InvalidRequestError(
                "training_policy.min_new_labels must be positive for new_labels"
            )
        due = new_label_count >= policy.min_new_labels
        reason_code = "new_label_threshold" if due else "not_enough_new_labels"
        reason = f"new labels={new_label_count}, threshold={policy.min_new_labels}"
    elif mode == "every_n_or_new_labels":
        if policy.interval < 1 or policy.min_new_labels < 1:
            raise InvalidRequestError(
                "every_n_or_new_labels requires positive interval and min_new_labels"
            )
        interval_due = last_training_round < 0 or round_index - last_training_round >= policy.interval
        labels_due = new_label_count >= policy.min_new_labels
        due = interval_due or labels_due
        reason_code = (
            "interval_or_new_label_threshold" if due else "training_conditions_not_met"
        )
        reason = f"interval_due={interval_due}, new_label_due={labels_due}"
    elif mode == "dataset_changed":
        due = bool(dataset_after_hash) and dataset_after_hash != dataset_before_hash
        reason_code = "dataset_changed" if due else "dataset_unchanged"
        reason = "dataset content hash changed" if due else "dataset content hash did not change"
    elif mode == "manual":
        normalized_action = manual_action.strip().lower()
        if normalized_action not in {"train", "skip"}:
            raise InvalidRequestError(
                "training_context.action must be train or skip for manual policy"
            )
        due = normalized_action == "train"
        reason_code = "manual_train" if due else "manual_skip"
        reason = "manual training decision supplied by the request"
    else:
        raise InvalidRequestError(
            "unsupported training_policy mode: "
            f"{policy.mode}; use never, always, every_n_rounds, new_labels, "
            "every_n_or_new_labels, dataset_changed, or manual"
        )

    if not due:
        return TrainingDecision(
            policy_id=policy.policy_id,
            policy_version=policy.version,
            action="skip",
            reason_code=reason_code,
            next_capability="",
            requested_mode=policy.train_mode,
            previous_checkpoint=previous_checkpoint,
            evaluated_inputs=inputs,
            policy_parameters=policy.to_dict(),
            reason=reason,
        )

    requested_mode = policy.train_mode.strip().lower()
    if requested_mode == "auto":
        action = "finetune" if previous_checkpoint.strip() else "train"
    elif requested_mode in {"train", "finetune"}:
        action = requested_mode
    else:
        raise InvalidRequestError("training_policy.train_mode must be auto, train, or finetune")
    return TrainingDecision(
        policy_id=policy.policy_id,
        policy_version=policy.version,
        action=action,
        reason_code=reason_code,
        next_capability=f"rits.{action}",
        requested_mode=policy.train_mode,
        previous_checkpoint=previous_checkpoint,
        evaluated_inputs=inputs,
        policy_parameters=policy.to_dict(),
        reason=reason,
    )


def resolve_snapshot_transition(
    source_snapshot: ModelSnapshot,
    training_decision: TrainingDecision,
    *,
    round_decision_hash: str,
    dataset_before_hash: str,
    dataset_after_hash: str,
    training_spec: Mapping[str, Any],
    next_snapshot: ModelSnapshot | None = None,
) -> SnapshotTransition:
    """Derive a deterministic successor snapshot contract for one round.

    A skipped training decision has an exact successor: the input snapshot is
    carried forward.  A train/finetune decision derives a stable planned
    snapshot ID from every replay input.  Once RitS has produced a real
    checkpoint, callers may provide that snapshot while retaining the same
    replay key.
    """

    if not isinstance(training_spec, Mapping):
        raise InvalidRequestError("training_spec must be a table")
    if not str(round_decision_hash).strip():
        raise InvalidRequestError("round_decision_hash must not be empty")
    normalized_spec = dict(training_spec)
    replay_inputs = {
        "schema": "tricycle.snapshot-replay/v1",
        "source_snapshot": source_snapshot.to_dict(),
        "training_action": training_decision.action,
        "round_decision_hash": str(round_decision_hash),
        "dataset_before_hash": dataset_before_hash,
        "dataset_after_hash": dataset_after_hash,
        "training_spec": normalized_spec,
    }
    replay_key = sha256_value(replay_inputs)

    if training_decision.action == "skip":
        if next_snapshot is not None and next_snapshot != source_snapshot:
            raise InvalidRequestError(
                "a skipped training decision must use the source snapshot as next_snapshot"
            )
        return SnapshotTransition(
            source_snapshot=source_snapshot,
            action="skip",
            status="carried_forward",
            next_snapshot=source_snapshot,
            replay_key=replay_key,
            round_decision_hash=str(round_decision_hash),
            dataset_before_hash=dataset_before_hash,
            dataset_after_hash=dataset_after_hash,
            training_spec=normalized_spec,
            rationale="RitS training was skipped; the source snapshot is carried forward unchanged",
        )

    if training_decision.action not in {"train", "finetune"}:
        raise InvalidRequestError(
            "training decision action must be skip, train, or finetune"
        )
    missing_fields = tuple(
        field_name
        for field_name in _REPLAY_REQUIRED_TRAINING_FIELDS
        if field_name not in normalized_spec or normalized_spec[field_name] in (None, "")
    )
    if missing_fields:
        raise InvalidRequestError(
            "training_spec is missing replay-critical fields: "
            + ", ".join(missing_fields)
        )

    if next_snapshot is None:
        successor = ModelSnapshot(
            snapshot_id=f"planned-{training_decision.action}-{replay_key[:24]}",
            metadata={
                "snapshot_status": "planned",
                "replay_key": replay_key,
                "source_snapshot_id": source_snapshot.snapshot_id,
            },
        )
        status = "planned"
        rationale = (
            "RitS training is scheduled; this deterministic planned snapshot is the successor "
            "until the training artifact is materialized"
        )
    else:
        if next_snapshot.snapshot_id == "unspecified":
            raise InvalidRequestError(
                "next_model_snapshot.snapshot_id must identify the RitS output snapshot"
            )
        successor = next_snapshot
        status = "realized"
        rationale = "RitS output snapshot was supplied for the deterministic replay contract"

    return SnapshotTransition(
        source_snapshot=source_snapshot,
        action=training_decision.action,
        status=status,
        next_snapshot=successor,
        replay_key=replay_key,
        round_decision_hash=str(round_decision_hash),
        dataset_before_hash=dataset_before_hash,
        dataset_after_hash=dataset_after_hash,
        training_spec=normalized_spec,
        rationale=rationale,
    )
