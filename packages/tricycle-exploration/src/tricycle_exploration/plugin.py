from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
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
    TraceSink,
    TraceStore,
    canonical_json,
    sha256_value,
    timestamped_run_directory,
)
from tricycle_core.errors import InvalidRequestError
from tricycle_core.io import file_sha256 as _sha256_file
from tricycle_core.io import write_delimited
from tricycle_core.toml import dump_toml, load_toml
from tricycle_sampling import (
    AGENT_ACQUISITION_STRATEGY,
    UNCERTAINTY_SCORE_KEYS,
    SamplingPlugin,
    SamplingRequest,
)

from .models import ExplorationRoundRequest
from .policies import decide_range, decide_training, resolve_snapshot_transition


def _write_tsv(path: Path, fields: tuple[str, ...], rows: Sequence[Mapping[str, object]]) -> None:
    write_delimited(path, fields, rows, delimiter="\t")




def _artifact_row(path: Path, root: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "path": str(path.relative_to(root)),
        "kind": "file",
        "size": stat.st_size,
        "sha256": _sha256_file(path),
    }


def _verify_artifact_manifest(manifest_path: Path, root: Path) -> None:
    manifest = load_toml(manifest_path)
    try:
        count = int(manifest.get("count", -1))
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError(f"artifact manifest has an invalid count: {manifest_path}") from exc
    if count < 1:
        raise InvalidRequestError(f"artifact manifest is empty: {manifest_path}")
    resolved_root = root.resolve()
    for index in range(count):
        row = manifest.get(f"artifact_{index:03d}")
        if not isinstance(row, Mapping):
            raise InvalidRequestError(
                f"artifact manifest is missing artifact_{index:03d}: {manifest_path}"
            )
        relative = Path(str(row.get("path", "")))
        artifact_path = (resolved_root / relative).resolve()
        try:
            artifact_path.relative_to(resolved_root)
        except ValueError as exc:
            raise InvalidRequestError(
                f"artifact manifest points outside the run directory: {relative}"
            ) from exc
        if not artifact_path.is_file():
            raise InvalidRequestError(f"artifact is missing: {artifact_path}")
        if int(row.get("size", -1)) != artifact_path.stat().st_size:
            raise InvalidRequestError(f"artifact size does not match manifest: {artifact_path}")
        if str(row.get("sha256", "")) != _sha256_file(artifact_path):
            raise InvalidRequestError(f"artifact hash does not match manifest: {artifact_path}")


def _strategy_kind(strategy: str) -> str | None:
    for kind in UNCERTAINTY_SCORE_KEYS:
        if strategy == f"{kind}-uncertainty":
            return kind
    return None


def _score_source(candidate: object, kind: str) -> str:
    payload = getattr(candidate, "payload", {})
    if any(key in payload for key in UNCERTAINTY_SCORE_KEYS[kind]):
        return "candidate.payload"
    if "posterior_probabilities" in payload:
        return "posterior_probabilities"
    return ""


def _trace_for(context: ExecutionContext, output_dir: Path) -> TraceSink:
    if context.trace_sink is not None:
        return context.trace_sink
    return TraceStore(output_dir, run_id=context.run_id)


def _relative_artifact(path: Path, root: Path) -> Artifact:
    return Artifact(path.name, str(path), kind="file", metadata={"relative_to_run": str(path.relative_to(root))})


class ExplorationPlugin(PluginBase):
    """Materialize a strict, replayable record for one exploration round."""

    _manifest = PluginManifest(
        plugin_id="tricycle.exploration",
        display_name="Exploration round provenance",
        version="0.1.0",
        kind=PluginKind.EXPLORATION,
        description=(
            "Record the candidate space, model scores, sampling decision, next-space policy, "
            "and RitS training gate for one exploration round, including an optional agent-authored "
            "acquisition function."
        ),
        capabilities=("round.execute",),
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
        if operation != "round":
            raise InvalidRequestError(f"unsupported exploration operation: {operation}")
        parsed = ExplorationRoundRequest.from_mapping(request)
        plan_request = parsed.to_dict()
        plan_request["resume"] = False
        return TaskPlan(
            task_id=f"{context.run_id}/exploration/{parsed.round_index:04d}",
            plugin_id=self.manifest.plugin_id,
            operation=operation,
            parameters={"request": plan_request},
            metadata={
                "exploration_id": parsed.exploration_id,
                "round_id": parsed.round_id,
                "round_index": parsed.round_index,
                "candidate_count": len(parsed.candidates),
                "dry_run": context.dry_run,
            },
        )

    def execute_round(
        self,
        request: ExplorationRoundRequest | Mapping[str, Any],
        context: ExecutionContext,
    ) -> TaskResult:
        parsed = request if isinstance(request, ExplorationRoundRequest) else ExplorationRoundRequest.from_mapping(request)
        output_dir = parsed.output_dir
        if output_dir is None:
            context_run_dir = context.metadata.get("run_dir")
            output_dir = (
                Path(str(context_run_dir))
                if context_run_dir
                else timestamped_run_directory(context.workdir / "runs", context.run_id)
            )
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        trace = _trace_for(context, output_dir)
        round_dir = output_dir / "rounds" / f"{parsed.round_index:04d}"

        if (round_dir / "round.toml").is_file():
            return self._reuse_round(parsed, round_dir, trace)
        if round_dir.exists() and any(round_dir.iterdir()):
            raise InvalidRequestError(f"exploration round directory is not empty: {round_dir}")

        try:
            round_dir.mkdir(parents=True, exist_ok=True)
            trace.emit(
                "round.started",
                round_id=parsed.round_id,
                step_id=f"{parsed.round_id}/start",
                capability_id="exploration.round",
                task_id=f"{context.run_id}/exploration/{parsed.round_index:04d}",
                payload={
                    "request_hash": parsed.request_hash,
                    "space_hash": parsed.space.fingerprint,
                    "candidate_count": len(parsed.candidates),
                    "budget": parsed.budget,
                    "strategy": parsed.strategy,
                    "seed": parsed.seed,
                },
                status="started",
            )

            excluded = dict(parsed.excluded_candidates)
            if parsed.exclude_seen:
                for candidate_id in parsed.seen_candidate_ids:
                    excluded.setdefault(candidate_id, "already_evaluated")
            eligible = tuple(
                candidate
                for candidate in parsed.candidates
                if candidate.candidate_id not in excluded
            )
            if parsed.budget > len(eligible):
                raise InvalidRequestError(
                    "sampling budget exceeds eligible candidates after exclusions: "
                    f"budget={parsed.budget}, eligible={len(eligible)}"
                )

            model_kind = _strategy_kind(parsed.strategy)
            needs_model_snapshot = model_kind is not None or parsed.strategy == AGENT_ACQUISITION_STRATEGY
            if needs_model_snapshot and parsed.model_snapshot.snapshot_id == "unspecified":
                raise InvalidRequestError(
                    "model-informed sampling requires model_snapshot.snapshot_id"
                )
            if parsed.strategy == AGENT_ACQUISITION_STRATEGY:
                if not parsed.model_snapshot.checkpoint_hash:
                    raise InvalidRequestError(
                        "agent-authored-acquisition requires model_snapshot.checkpoint_hash"
                    )
                if not parsed.observed_metrics:
                    raise InvalidRequestError(
                        "agent-authored-acquisition requires observed model evaluation metrics"
                    )
                acquisition_input = parsed.metadata.get("agent_acquisition", {})
                acquisition_context = (
                    acquisition_input.get("context", {})
                    if isinstance(acquisition_input, Mapping)
                    else {}
                )
                agent_metrics = (
                    acquisition_context.get("model_metrics", {})
                    if isinstance(acquisition_context, Mapping)
                    else {}
                )
                if not isinstance(agent_metrics, Mapping):
                    raise InvalidRequestError(
                        "metadata.agent_acquisition.context.model_metrics must be a mapping"
                    )
                shared_metrics = set(agent_metrics) & set(parsed.observed_metrics)
                if not shared_metrics:
                    raise InvalidRequestError(
                        "agent-authored model_metrics must repeat at least one observed_metrics value"
                    )
                mismatched_metrics = sorted(
                    key
                    for key in shared_metrics
                    if canonical_json(agent_metrics[key])
                    != canonical_json(parsed.observed_metrics[key])
                )
                if mismatched_metrics:
                    raise InvalidRequestError(
                        "agent-authored model_metrics disagree with observed_metrics: "
                        + ", ".join(mismatched_metrics)
                    )
            # Resolve policy inputs before any round artifact is materialized.
            # Invalid training recipes therefore fail without leaving a
            # partial candidate/score trajectory behind.
            range_decision = decide_range(
                parsed.range_policy,
                parsed.space,
                parsed.next_space,
                parsed.observed_metrics,
            )
            training_decision = decide_training(
                parsed.training_policy,
                round_index=parsed.round_index,
                last_training_round=parsed.last_training_round,
                new_label_count=parsed.new_label_count,
                dataset_before_hash=parsed.dataset_before_hash,
                dataset_after_hash=parsed.dataset_after_hash,
                previous_checkpoint=parsed.previous_checkpoint,
                manual_action=parsed.training_action,
            )

            sampling_metadata = dict(parsed.metadata)
            if parsed.strategy == AGENT_ACQUISITION_STRATEGY:
                sampling_metadata["round_context"] = {
                    "round_id": parsed.round_id,
                    "round_index": parsed.round_index,
                    "space_hash": parsed.space.fingerprint,
                    "model_snapshot": parsed.model_snapshot.to_dict(),
                    "observed_metrics": dict(parsed.observed_metrics),
                }
            sampling_request = SamplingRequest(
                candidates=eligible,
                budget=parsed.budget,
                strategy=parsed.strategy,
                seed=parsed.seed,
                metadata=sampling_metadata,
            )
            sampling = SamplingPlugin()
            sampling_plan = sampling.plan_select(sampling_request, context)
            sampling_result = sampling.select(sampling_request)
            score_entries = sampling_plan.parameters.get("candidate_scores", ())
            score_map = {
                str(item["candidate_id"]): float(item["score"])
                for item in score_entries
                if isinstance(item, Mapping) and "candidate_id" in item and "score" in item
            }
            acquisition_cost_map = {
                str(item["candidate_id"]): float(item["estimated_cost"])
                for item in score_entries
                if isinstance(item, Mapping)
                and "candidate_id" in item
                and "estimated_cost" in item
            }
            acquisition_function = sampling_plan.parameters.get("acquisition_function")
            acquisition_path: Path | None = None
            if isinstance(acquisition_function, Mapping):
                acquisition_path = round_dir / "acquisition-function.toml"
                dump_toml(
                    acquisition_path,
                    {
                        "schema": acquisition_function.get("schema", ""),
                        "strategy": acquisition_function.get("strategy", ""),
                        "strategy_version": acquisition_function.get("strategy_version", ""),
                        "function_id": acquisition_function.get("function_id", ""),
                        "function_version": acquisition_function.get("version", ""),
                        "authored_by": acquisition_function.get("authored_by", ""),
                        "expression_language": acquisition_function.get("expression_language", ""),
                        "expression": acquisition_function.get("expression", ""),
                        "cost_expression": acquisition_function.get("cost_expression", ""),
                        "rationale": acquisition_function.get("rationale", ""),
                        "author_notes": acquisition_function.get("author_notes", ""),
                        "function_hash": acquisition_function.get("function_hash", ""),
                        "context_json": canonical_json(acquisition_function.get("context", {})),
                        "effective_context_json": canonical_json(
                            acquisition_function.get("effective_context", {})
                        ),
                        "evidence_json": canonical_json(acquisition_function.get("evidence", {})),
                        "cost_summary_json": canonical_json(
                            acquisition_function.get("cost_summary", {})
                        ),
                    },
                )
                trace.emit(
                    "acquisition.function.authored",
                    round_id=parsed.round_id,
                    step_id=f"{parsed.round_id}/acquisition-function",
                    capability_id="exploration.round",
                    payload={
                        "path": str(acquisition_path.relative_to(output_dir)),
                        "sha256": _sha256_file(acquisition_path),
                        "function_hash": acquisition_function.get("function_hash", ""),
                        "function_id": acquisition_function.get("function_id", ""),
                        "expression": acquisition_function.get("expression", ""),
                        "cost_expression": acquisition_function.get("cost_expression", ""),
                    },
                    status="succeeded",
                )
            input_index = {
                candidate.candidate_id: index for index, candidate in enumerate(parsed.candidates)
            }
            if score_map:
                ranked = sorted(
                    eligible,
                    key=lambda candidate: (-score_map[candidate.candidate_id], candidate.candidate_id),
                )
            else:
                ranked = list(eligible)
            score_rank = {
                candidate.candidate_id: index + 1 for index, candidate in enumerate(ranked)
            }
            selection_order = {
                candidate.candidate_id: index + 1
                for index, candidate in enumerate(sampling_result.selected)
            }
            selected_ids = set(selection_order)

            pool_rows = [
                {
                    "input_index": index,
                    "candidate_id": candidate.candidate_id,
                    "payload_json": canonical_json(candidate.payload),
                    "space_id": parsed.space.space_id,
                    "eligible": candidate.candidate_id not in excluded,
                    "exclusion_reason": excluded.get(candidate.candidate_id, ""),
                }
                for index, candidate in enumerate(parsed.candidates)
            ]
            score_rows = [
                {
                    "input_index": input_index[candidate.candidate_id],
                    "candidate_id": candidate.candidate_id,
                    "score": score_map.get(candidate.candidate_id, ""),
                    "score_type": sampling_plan.parameters.get("score_field", ""),
                    "score_source": (
                        _score_source(candidate, model_kind)
                        if model_kind
                        else "agent-authored-expression"
                        if parsed.strategy == AGENT_ACQUISITION_STRATEGY
                        else ""
                    ),
                    "model_snapshot_id": parsed.model_snapshot.snapshot_id if needs_model_snapshot else "",
                    "model_checkpoint_hash": parsed.model_snapshot.checkpoint_hash if needs_model_snapshot else "",
                    "model_score_artifact": parsed.model_snapshot.score_artifact if needs_model_snapshot else "",
                    "acquisition_cost": acquisition_cost_map.get(candidate.candidate_id, ""),
                    "eligible": candidate.candidate_id not in excluded,
                }
                for candidate in parsed.candidates
            ]
            decision_rows = []
            for index, candidate in enumerate(parsed.candidates):
                candidate_id = candidate.candidate_id
                is_excluded = candidate_id in excluded
                selected = candidate_id in selected_ids
                decision_rows.append(
                    {
                        "input_index": index,
                        "candidate_id": candidate_id,
                        "eligible": not is_excluded,
                        "exclusion_reason": excluded.get(candidate_id, ""),
                        "score": score_map.get(candidate_id, ""),
                        "score_rank": score_rank.get(candidate_id, ""),
                        "tie_break_key": candidate_id if score_map else str(index),
                        "estimated_cost": acquisition_cost_map.get(candidate_id, ""),
                        "selected": selected,
                        "selection_order": selection_order.get(candidate_id, ""),
                        "decision_reason": (
                            excluded.get(candidate_id, "")
                            if is_excluded
                            else "selected_by_strategy"
                            if selected
                            else "outside_budget"
                        ),
                    }
                )
            selected_rows = [
                {
                    "selection_order": selection_order[candidate.candidate_id],
                    "candidate_id": candidate.candidate_id,
                    "payload_json": canonical_json(candidate.payload),
                    "score": score_map.get(candidate.candidate_id, ""),
                    "score_rank": score_rank.get(candidate.candidate_id, ""),
                    "estimated_cost": acquisition_cost_map.get(candidate.candidate_id, ""),
                }
                for candidate in sampling_result.selected
            ]

            space_path = round_dir / "space.toml"
            dump_toml(
                space_path,
                {
                    "schema": "tricycle.exploration.space/v1",
                    "space_id": parsed.space.space_id,
                    "version": parsed.space.version,
                    "definition_hash": parsed.space.fingerprint,
                    "definition_json": canonical_json(parsed.space.definition),
                    "source_artifacts": list(parsed.space.source_artifacts),
                },
            )
            trace.emit(
                "space.materialized",
                round_id=parsed.round_id,
                step_id=f"{parsed.round_id}/space",
                capability_id="exploration.round",
                payload={"path": str(space_path.relative_to(output_dir)), "sha256": _sha256_file(space_path)},
                status="succeeded",
            )

            pool_path = round_dir / "candidate-pool.tsv"
            _write_tsv(
                pool_path,
                ("input_index", "candidate_id", "payload_json", "space_id", "eligible", "exclusion_reason"),
                pool_rows,
            )
            trace.emit(
                "candidate.pool.materialized",
                round_id=parsed.round_id,
                step_id=f"{parsed.round_id}/candidate-pool",
                capability_id="exploration.round",
                payload={
                    "path": str(pool_path.relative_to(output_dir)),
                    "sha256": _sha256_file(pool_path),
                    "candidate_count": len(pool_rows),
                    "eligible_count": len(eligible),
                },
                status="succeeded",
            )

            scores_path = round_dir / "model-scores.tsv"
            _write_tsv(
                scores_path,
                (
                    "input_index",
                    "candidate_id",
                    "score",
                    "score_type",
                    "score_source",
                    "model_snapshot_id",
                    "model_checkpoint_hash",
                    "model_score_artifact",
                    "acquisition_cost",
                    "eligible",
                ),
                score_rows,
            )
            trace.emit(
                "model.scored",
                round_id=parsed.round_id,
                step_id=f"{parsed.round_id}/model-score",
                capability_id="exploration.round",
                payload={
                    "path": str(scores_path.relative_to(output_dir)),
                    "sha256": _sha256_file(scores_path),
                    "model_snapshot": parsed.model_snapshot.to_dict(),
                    "score_type": sampling_plan.parameters.get("score_field", ""),
                },
                status="succeeded",
            )

            decisions_path = round_dir / "selection-decisions.tsv"
            _write_tsv(
                decisions_path,
                (
                    "input_index",
                    "candidate_id",
                    "eligible",
                    "exclusion_reason",
                    "score",
                    "score_rank",
                    "tie_break_key",
                    "estimated_cost",
                    "selected",
                    "selection_order",
                    "decision_reason",
                ),
                decision_rows,
            )
            selected_path = round_dir / "selected-candidates.tsv"
            _write_tsv(
                selected_path,
                (
                    "selection_order",
                    "candidate_id",
                    "payload_json",
                    "score",
                    "score_rank",
                    "estimated_cost",
                ),
                selected_rows,
            )
            sampling_decision_hash = sha256_value(
                {
                    "strategy": parsed.strategy,
                    "strategy_version": sampling_plan.metadata.get("strategy_version", ""),
                    "budget": parsed.budget,
                    "seed": parsed.seed,
                    "candidate_pool_hash": _sha256_file(pool_path),
                    "model_scores_hash": _sha256_file(scores_path),
                    "selection_decisions_hash": _sha256_file(decisions_path),
                    "acquisition_function_hash": (
                        acquisition_function.get("function_hash", "")
                        if isinstance(acquisition_function, Mapping)
                        else ""
                    ),
                    "selected_ids": list(sampling_result.selected_ids),
                    "remaining_ids": [
                        candidate.candidate_id for candidate in sampling_result.remaining
                    ],
                }
            )
            trace.emit(
                "sampling.selected",
                round_id=parsed.round_id,
                step_id=f"{parsed.round_id}/sampling",
                capability_id="exploration.round",
                payload={
                    "strategy": parsed.strategy,
                    "strategy_version": sampling_plan.metadata.get("strategy_version", ""),
                    "selection": sampling_plan.metadata.get("selection", ""),
                    "seed": parsed.seed,
                    "budget": parsed.budget,
                    "selected_ids": list(sampling_result.selected_ids),
                    "candidate_pool_hash": _sha256_file(pool_path),
                    "sampling_decision_hash": sampling_decision_hash,
                    "acquisition_function_hash": (
                        acquisition_function.get("function_hash", "")
                        if isinstance(acquisition_function, Mapping)
                        else ""
                    ),
                    "decision_table": str(decisions_path.relative_to(output_dir)),
                },
                status="succeeded",
            )

            range_path = round_dir / "range-decision.toml"
            dump_toml(
                range_path,
                {
                    "schema": "tricycle.exploration.range-decision/v1",
                    "round_id": parsed.round_id,
                    "policy_id": range_decision.policy_id,
                    "policy_version": range_decision.policy_version,
                    "method": range_decision.method,
                    "input_space_hash": parsed.space.fingerprint,
                    "next_space_hash": range_decision.next_space.fingerprint,
                    "parameters_json": canonical_json(range_decision.parameters),
                    "observed_metrics_json": canonical_json(range_decision.observed_metrics),
                    "next_space_json": canonical_json(range_decision.next_space.to_dict()),
                    "rationale": range_decision.rationale,
                    "decision_hash": range_decision.decision_hash,
                },
            )
            trace.emit(
                "range.decided",
                round_id=parsed.round_id,
                step_id=f"{parsed.round_id}/range",
                capability_id="exploration.round",
                payload={
                    "path": str(range_path.relative_to(output_dir)),
                    "sha256": _sha256_file(range_path),
                    "decision": range_decision.to_dict(),
                },
                status="succeeded",
            )

            round_decision_hash = sha256_value(
                {
                    "schema": "tricycle.exploration.round-decision/v1",
                    "request_hash": parsed.request_hash,
                    "sampling_decision_hash": sampling_decision_hash,
                    "range_decision_hash": range_decision.decision_hash,
                    "training_decision_hash": training_decision.decision_hash,
                    "observed_metrics": dict(parsed.observed_metrics),
                    "dataset_before_hash": parsed.dataset_before_hash,
                    "dataset_after_hash": parsed.dataset_after_hash,
                }
            )
            snapshot_transition = resolve_snapshot_transition(
                parsed.model_snapshot,
                training_decision,
                round_decision_hash=round_decision_hash,
                dataset_before_hash=parsed.dataset_before_hash,
                dataset_after_hash=parsed.dataset_after_hash,
                training_spec=parsed.training_spec,
                next_snapshot=parsed.next_model_snapshot,
            )
            training_path = round_dir / "training-decision.toml"
            dump_toml(
                training_path,
                {
                    "schema": "tricycle.exploration.training-decision/v1",
                    "round_id": parsed.round_id,
                    "policy_id": training_decision.policy_id,
                    "policy_version": training_decision.policy_version,
                    "action": training_decision.action,
                    "reason_code": training_decision.reason_code,
                    "reason": training_decision.reason,
                    "next_capability": training_decision.next_capability,
                    "requested_mode": training_decision.requested_mode,
                    "previous_checkpoint": training_decision.previous_checkpoint,
                    "evaluated_inputs_json": canonical_json(training_decision.evaluated_inputs),
                    "policy_json": canonical_json(training_decision.policy_parameters),
                    "decision_hash": training_decision.decision_hash,
                },
            )
            trace.emit(
                "training.decided",
                round_id=parsed.round_id,
                step_id=f"{parsed.round_id}/training-gate",
                capability_id="exploration.round",
                payload={
                    "path": str(training_path.relative_to(output_dir)),
                    "sha256": _sha256_file(training_path),
                    "decision": training_decision.to_dict(),
                },
                status="succeeded",
            )

            snapshot_path = round_dir / "snapshot-transition.toml"
            dump_toml(
                snapshot_path,
                {
                    "schema": "tricycle.exploration.snapshot-transition/v1",
                    "round_id": parsed.round_id,
                    "action": snapshot_transition.action,
                    "status": snapshot_transition.status,
                    "source_snapshot_hash": snapshot_transition.source_snapshot.fingerprint,
                    "next_snapshot_hash": snapshot_transition.next_snapshot.fingerprint,
                    "replay_key": snapshot_transition.replay_key,
                    "round_decision_hash": snapshot_transition.round_decision_hash,
                    "dataset_before_hash": snapshot_transition.dataset_before_hash,
                    "dataset_after_hash": snapshot_transition.dataset_after_hash,
                    "source_snapshot_json": canonical_json(
                        snapshot_transition.source_snapshot.to_dict()
                    ),
                    "next_snapshot_json": canonical_json(
                        snapshot_transition.next_snapshot.to_dict()
                    ),
                    "training_spec_json": canonical_json(snapshot_transition.training_spec),
                    "rationale": snapshot_transition.rationale,
                    "transition_hash": snapshot_transition.transition_hash,
                },
            )
            trace.emit(
                "snapshot.transitioned",
                round_id=parsed.round_id,
                step_id=f"{parsed.round_id}/snapshot",
                capability_id="exploration.round",
                payload={
                    "path": str(snapshot_path.relative_to(output_dir)),
                    "sha256": _sha256_file(snapshot_path),
                    "transition": snapshot_transition.to_dict(),
                },
                status="succeeded",
            )

            manifest_dir = round_dir / "artifacts"
            manifest_path = manifest_dir / "manifest.toml"
            tracked_paths = (
                space_path,
                pool_path,
                scores_path,
                decisions_path,
                selected_path,
                range_path,
                training_path,
                snapshot_path,
                *((acquisition_path,) if acquisition_path is not None else ()),
            )
            manifest_values: dict[str, object] = {
                "schema": "tricycle.artifact-manifest/v1",
                "round_id": parsed.round_id,
                "count": len(tracked_paths),
            }
            for index, path in enumerate(tracked_paths):
                manifest_values[f"artifact_{index:03d}"] = _artifact_row(path, output_dir)
            dump_toml(manifest_path, manifest_values)

            round_path = round_dir / "round.toml"
            dump_toml(
                round_path,
                {
                    "schema": "tricycle.exploration.round/v1",
                    "exploration_id": parsed.exploration_id,
                    "round_id": parsed.round_id,
                    "round_index": parsed.round_index,
                    "parent_round_id": parsed.parent_round_id,
                    "request_hash": parsed.request_hash,
                    "space_hash": parsed.space.fingerprint,
                    "candidate_pool_hash": _sha256_file(pool_path),
                    "model_snapshot_id": parsed.model_snapshot.snapshot_id,
                    "model_checkpoint_hash": parsed.model_snapshot.checkpoint_hash,
                    "model_score_artifact": parsed.model_snapshot.score_artifact,
                    "model_source_run_id": parsed.model_snapshot.source_run_id,
                    "model_snapshot_hash": parsed.model_snapshot.fingerprint,
                    "strategy": parsed.strategy,
                    "strategy_version": sampling_plan.metadata.get("strategy_version", ""),
                    "acquisition_function_id": (
                        acquisition_function.get("function_id", "")
                        if isinstance(acquisition_function, Mapping)
                        else ""
                    ),
                    "acquisition_function_hash": (
                        acquisition_function.get("function_hash", "")
                        if isinstance(acquisition_function, Mapping)
                        else ""
                    ),
                    "budget": parsed.budget,
                    "seed": parsed.seed if parsed.seed is not None else "",
                    "candidate_count": len(parsed.candidates),
                    "eligible_count": len(eligible),
                    "excluded_count": len(parsed.candidates) - len(eligible),
                    "selected_count": len(sampling_result.selected),
                    "remaining_count": len(sampling_result.remaining),
                    "selected_ids_json": canonical_json(list(sampling_result.selected_ids)),
                    "remaining_ids_json": canonical_json(
                        [candidate.candidate_id for candidate in sampling_result.remaining]
                    ),
                    "range_decision_hash": range_decision.decision_hash,
                    "training_decision_hash": training_decision.decision_hash,
                    "round_decision_hash": round_decision_hash,
                    "snapshot_transition_hash": snapshot_transition.transition_hash,
                    "snapshot_replay_key": snapshot_transition.replay_key,
                    "source_snapshot_id": snapshot_transition.source_snapshot.snapshot_id,
                    "source_snapshot_hash": snapshot_transition.source_snapshot.fingerprint,
                    "next_snapshot_id": snapshot_transition.next_snapshot.snapshot_id,
                    "next_snapshot_hash": snapshot_transition.next_snapshot.fingerprint,
                    "training_action": training_decision.action,
                    "training_reason_code": training_decision.reason_code,
                    "next_capability": training_decision.next_capability,
                    "artifacts_manifest": str(manifest_path.relative_to(output_dir)),
                    "artifacts_manifest_hash": _sha256_file(manifest_path),
                },
            )
            trace.emit(
                "round.closed",
                round_id=parsed.round_id,
                step_id=f"{parsed.round_id}/close",
                capability_id="exploration.round",
                payload={
                    "round_path": str(round_path.relative_to(output_dir)),
                    "round_hash": _sha256_file(round_path),
                    "artifact_manifest": str(manifest_path.relative_to(output_dir)),
                    "artifact_manifest_hash": _sha256_file(manifest_path),
                    "selected_ids": list(sampling_result.selected_ids),
                    "training_action": training_decision.action,
                    "next_capability": training_decision.next_capability,
                    "snapshot_transition_hash": snapshot_transition.transition_hash,
                    "snapshot_replay_key": snapshot_transition.replay_key,
                    "next_snapshot_id": snapshot_transition.next_snapshot.snapshot_id,
                },
                status="succeeded",
            )
            return TaskResult(
                task_id=f"{context.run_id}/exploration/{parsed.round_index:04d}",
                plugin_id=self.manifest.plugin_id,
                status=TaskStatus.SUCCEEDED,
                outputs=(
                    Artifact(f"round-{parsed.round_index:04d}", str(round_dir), kind="directory"),
                    _relative_artifact(selected_path, output_dir),
                    _relative_artifact(decisions_path, output_dir),
                    _relative_artifact(training_path, output_dir),
                    _relative_artifact(snapshot_path, output_dir),
                    *(
                        (_relative_artifact(acquisition_path, output_dir),)
                        if acquisition_path is not None
                        else ()
                    ),
                ),
                message="exploration round provenance recorded",
                metadata={
                    "exploration_id": parsed.exploration_id,
                    "round_id": parsed.round_id,
                    "round_index": parsed.round_index,
                    "request_hash": parsed.request_hash,
                    "space_hash": parsed.space.fingerprint,
                    "candidate_pool_hash": _sha256_file(pool_path),
                    "candidate_count": len(parsed.candidates),
                    "eligible_count": len(eligible),
                    "excluded_count": len(parsed.candidates) - len(eligible),
                    "selected_ids": list(sampling_result.selected_ids),
                    "acquisition_function_id": (
                        acquisition_function.get("function_id", "")
                        if isinstance(acquisition_function, Mapping)
                        else ""
                    ),
                    "acquisition_function_hash": (
                        acquisition_function.get("function_hash", "")
                        if isinstance(acquisition_function, Mapping)
                        else ""
                    ),
                    "remaining_ids": [candidate.candidate_id for candidate in sampling_result.remaining],
                    "range_decision": range_decision.to_dict(),
                    "training_decision": training_decision.to_dict(),
                    "snapshot_transition": snapshot_transition.to_dict(),
                    "next_action": training_decision.next_capability,
                    "training_next_action": training_decision.next_capability,
                    "round_dir": str(round_dir),
                    "artifacts_manifest": str(manifest_path),
                },
            )
        except Exception as exc:
            trace.emit(
                "round.failed",
                round_id=parsed.round_id,
                step_id=f"{parsed.round_id}/failure",
                capability_id="exploration.round",
                payload={"error_type": type(exc).__name__, "error": str(exc)},
                status="failed",
            )
            raise

    def _reuse_round(
        self,
        request: ExplorationRoundRequest,
        round_dir: Path,
        trace: TraceSink,
    ) -> TaskResult:
        if not request.resume:
            raise InvalidRequestError(
                f"exploration round already exists; set resume=true to reuse it: {round_dir}"
            )
        state = load_toml(round_dir / "round.toml")
        if state.get("request_hash") != request.request_hash:
            raise InvalidRequestError("existing exploration round request hash does not match")
        output_dir = round_dir.parent.parent
        manifest_path = output_dir / str(state.get("artifacts_manifest", ""))
        if not manifest_path.is_file():
            raise InvalidRequestError(f"existing exploration round has no artifact manifest: {manifest_path}")
        if str(state.get("artifacts_manifest_hash", "")) != _sha256_file(manifest_path):
            raise InvalidRequestError("existing exploration round artifact manifest hash does not match")
        _verify_artifact_manifest(manifest_path, output_dir)
        try:
            selected_ids = json.loads(str(state.get("selected_ids_json", "[]")))
            remaining_ids = json.loads(str(state.get("remaining_ids_json", "[]")))
        except json.JSONDecodeError as exc:
            raise InvalidRequestError("existing exploration round has invalid candidate lists") from exc
        trace.emit(
            "round.reused",
            round_id=request.round_id,
            step_id=f"{request.round_id}/reuse",
            capability_id="exploration.round",
            payload={
                "round_path": str(round_dir),
                "request_hash": request.request_hash,
                "selected_ids": selected_ids,
                "remaining_ids": remaining_ids,
            },
            status="succeeded",
        )
        selected_path = round_dir / "selected-candidates.tsv"
        decisions_path = round_dir / "selection-decisions.tsv"
        training_path = round_dir / "training-decision.toml"
        snapshot_path = round_dir / "snapshot-transition.toml"
        return TaskResult(
            task_id=f"{request.exploration_id}/exploration/{request.round_index:04d}",
            plugin_id=self.manifest.plugin_id,
            status=TaskStatus.SUCCEEDED,
            outputs=(
                Artifact(f"round-{request.round_index:04d}", str(round_dir), kind="directory"),
                _relative_artifact(selected_path, round_dir.parent.parent),
                _relative_artifact(decisions_path, round_dir.parent.parent),
                _relative_artifact(training_path, round_dir.parent.parent),
                _relative_artifact(snapshot_path, round_dir.parent.parent),
                *(
                    (_relative_artifact(round_dir / "acquisition-function.toml", round_dir.parent.parent),)
                    if (round_dir / "acquisition-function.toml").is_file()
                    else ()
                ),
            ),
            message="existing exploration round provenance reused",
            metadata={
                "exploration_id": request.exploration_id,
                "round_id": request.round_id,
                "round_index": request.round_index,
                "request_hash": request.request_hash,
                "selected_ids": selected_ids,
                "remaining_ids": remaining_ids,
                "acquisition_function_id": state.get("acquisition_function_id", ""),
                "acquisition_function_hash": state.get("acquisition_function_hash", ""),
                "training_action": state.get("training_action", ""),
                "training_reason_code": state.get("training_reason_code", ""),
                "snapshot_transition_hash": state.get("snapshot_transition_hash", ""),
                "snapshot_replay_key": state.get("snapshot_replay_key", ""),
                "next_snapshot_id": state.get("next_snapshot_id", ""),
                "next_action": state.get("next_capability", ""),
                "round_dir": str(round_dir),
            },
        )
