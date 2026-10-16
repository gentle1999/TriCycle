"""Persist a replay acquisition using the sampling API and its validated scores."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from tricycle_core import ExecutionContext
from tricycle_core import record_io as rio
from tricycle_core.record_io import dump_toml, file_sha256, load_toml, write_delimited
from tricycle_core.validation import validate_fields

from tricycle_sampling import SamplingPlugin, SamplingRequest


def select_round(request: dict[str, Any]) -> dict[str, Any]:
    """Select locally and commit one auditable, idempotent decision directory."""
    sampling_fields = {"candidates", "budget", "strategy", "seed", "metadata"}
    validate_fields(request, sampling_fields | {
        "exploration_id", "round_index", "output_dir", "resume", "space",
        "model_snapshot", "observed_metrics", "training_policy",
    }, context="replay selection")
    parsed = SamplingRequest.from_mapping({
        key: value for key, value in request.items() if key in sampling_fields
    })
    snapshot = request["model_snapshot"]
    if not snapshot.get("checkpoint_hash") or not snapshot.get("score_artifact"):
        raise ValueError("Replay selection requires checkpoint and prediction hashes")
    observed = request["observed_metrics"]
    supplied = parsed.metadata["agent_acquisition"]["context"]["model_metrics"]
    shared = set(observed) & set(supplied)
    if not shared or any(observed[key] != supplied[key] for key in shared):
        raise ValueError("Acquisition model metrics differ from observed metrics")
    request_hash = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    round_dir = Path(request["output_dir"]) / "rounds" / f"{request['round_index']:04d}"
    sampling = SamplingPlugin()
    plan = sampling.plan_select(parsed, ExecutionContext("replay-selection", round_dir, True))
    result = sampling.select(parsed)
    definition = plan.parameters["acquisition_function"]
    record = {
        "request_sha256": request_hash,
        "selected_ids": list(result.selected_ids),
        "acquisition_function_hash": definition["function_hash"],
    }
    if rio.exists(round_dir):
        state = load_toml(round_dir / "round.toml")
        if any(state.get(key) != value for key, value in record.items()):
            raise ValueError("Committed replay selection differs from this request")
        for name, expected in state["files"].items():
            if file_sha256(round_dir / name) != expected:
                raise ValueError(f"Committed replay selection artifact changed: {name}")
        return record
    rio.mkdir(round_dir.parent, parents=True, exist_ok=True)
    def commit(output: Path) -> None:
        artifact = {
            key: value for key, value in definition.items()
            if key not in {"context", "effective_context", "evidence", "cost_summary"}
        }
        artifact.update({
            key + "_json": json.dumps(definition.get(key, {}), sort_keys=True, allow_nan=False)
            for key in ("context", "effective_context", "evidence", "cost_summary")
        })
        dump_toml(output / "acquisition-function.toml", artifact)
        scores = [
            {
                "candidate_id": item["candidate_id"],
                "score": item["score"],
                "acquisition_cost": item["estimated_cost"],
                "model_checkpoint_hash": snapshot["checkpoint_hash"],
                "model_score_artifact": snapshot["score_artifact"],
            }
            for item in plan.parameters["candidate_scores"]
        ]
        write_delimited(
            output / "model-scores.tsv",
            ("candidate_id", "score", "acquisition_cost", "model_checkpoint_hash", "model_score_artifact"),
            scores, delimiter="\t",
        )
        write_delimited(
            output / "selected-candidates.tsv", ("rank", "candidate_id"),
            [{"rank": index + 1, "candidate_id": identifier}
             for index, identifier in enumerate(result.selected_ids)], delimiter="\t",
        )
        dump_toml(output / "round.toml", {
            **record, "files": {path.name: file_sha256(path) for path in rio.iterdir(output)},
        })

    if rio.location(round_dir):
        with rio.transaction(round_dir):
            rio.mkdir(round_dir)
            commit(round_dir)
    else:
        with tempfile.TemporaryDirectory(prefix=".selection-", dir=round_dir.parent) as temporary:
            output = Path(temporary)
            commit(output)
            output.rename(round_dir)
    return record
