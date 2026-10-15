"""Coverage, cost and feedback guidance for adaptive acquisition."""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from typing import Any

from tricycle_sampling.replay import core_hours as replay_core_hours

NAME = "coverage-cost-guided-v1"
SCHEMA = "tricycle.acquisition-guidance/v1"
EXPRESSION = (
    "(0.5 * sampled_novelty + 0.5 * (1 - pool_local_density)) / "
    "max(1, (product_count * mean_product_atom_count) / context.coverage_cost.cost_proxy_median)"
)
COST_EXPRESSION = "estimated_core_hours"


def protocol() -> dict[str, Any]:
    return {
        "strategy": "intelligent", "protocol_id": NAME, "version": "1.0.0",
        "origin": "TriCycle adaptive acquisition using structure coverage, calibrated costs and model feedback",
        "initial_expression": EXPRESSION,
        "cost_expression": COST_EXPRESSION,
        "feedback": True, "review": True,
        "rule": "Agent-guided coverage and cost efficiency with revealed-label feedback and one batch review.",
        "adaptation": "Initialization uses weights 0.5/0.5 and exponent 1; feedback informs retention, adjustment or replacement.",
        "tie_break": "candidate_id ascending",
    }


def _inputs(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = []
    for candidate in candidates:
        p = candidate["payload"]
        values = {key: float(p[key]) for key in (
            "sampled_novelty", "pool_local_density", "product_count",
            "mean_product_atom_count", "estimated_core_hours")}
        if not all(math.isfinite(v) for v in values.values()):
            raise ValueError("non-finite coverage/cost input")
        if any(not 0 <= values[key] <= 1 for key in ("sampled_novelty", "pool_local_density")):
            raise ValueError("coverage/cost novelty/density must be in [0, 1]")
        if any(values[key] <= 0 for key in ("product_count", "mean_product_atom_count", "estimated_core_hours")):
            raise ValueError("coverage/cost requires positive workloads and calibrated cost estimates")
        if not values['product_count'].is_integer() or not math.isfinite(values['product_count'] * values['mean_product_atom_count']):
            raise ValueError("invalid coverage/cost structural workload")
        entries.append({"candidate_id": candidate["candidate_id"], **values})
    ids = [entry["candidate_id"] for entry in entries]
    if not entries or len(ids) != len(set(ids)):
        raise ValueError("coverage/cost requires a nonempty unique candidate pool")
    return sorted(entries, key=lambda entry: entry["candidate_id"])


def _batch(ranking: list[dict[str, Any]], entries: list[dict[str, Any]], budget: int) -> dict[str, Any]:
    lookup = {entry["candidate_id"]: entry for entry in entries}
    chosen = [lookup[row["candidate_id"]] for row in ranking[:budget]]
    candidates = [{"candidate_id": entry["candidate_id"], "payload": entry} for entry in entries]
    return {
        **replay_core_hours.ranking_diagnostics(ranking, budget, candidates),
        "mean_novelty": statistics.mean(entry["sampled_novelty"] for entry in chosen),
        "mean_sparsity": statistics.mean(1 - entry["pool_local_density"] for entry in chosen),
        "structural_cost_proxy": sum(entry["product_count"] * entry["mean_product_atom_count"] for entry in chosen),
    }


def build(candidates: list[dict[str, Any]], budget: int) -> dict[str, Any]:
    entries = _inputs(candidates)
    if not 0 < budget <= len(entries):
        raise ValueError("invalid coverage/cost reference budget")
    median = statistics.median(entry["product_count"] * entry["mean_product_atom_count"] for entry in entries)
    ranking = [{
        "candidate_id": entry["candidate_id"],
        "score": (.5 * entry["sampled_novelty"] + .5 * (1 - entry["pool_local_density"])) / max(
            1., entry["product_count"] * entry["mean_product_atom_count"] / median),
    } for entry in entries]
    ranking.sort(key=lambda row: (-row["score"], row["candidate_id"]))
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {
        "schema": SCHEMA, "protocol": protocol(), "budget": budget,
        "candidate_inputs_sha256": hashlib.sha256(encoded).hexdigest(),
        "context": {"cost_proxy_median": median},
        "ranking": ranking, "selected_ids": [row["candidate_id"] for row in ranking[:budget]],
        "diagnostics": _batch(ranking, entries, budget),
        "limits": "Reference batch is unexecuted; no labels or evaluation outcome are available. Agent ranking is not overridden.",
    }


def verify(record: dict[str, Any], candidates: list[dict[str, Any]], budget: int) -> None:
    if record != build(candidates, budget):
        raise ValueError("coverage/cost reference differs from the current full pool")


def compact(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in ("schema", "protocol", "context", "diagnostics", "limits")}


def compare(record: dict[str, Any], ranking: list[dict[str, Any]],
            candidates: list[dict[str, Any]], budget: int) -> dict[str, Any]:
    verify(record, candidates, budget)
    entries = _inputs(candidates)
    ids = [row["candidate_id"] for row in ranking]
    if len(ids) != len(set(ids)) or set(ids) != {entry["candidate_id"] for entry in entries}:
        raise ValueError("proposal ranking must cover the complete coverage/cost pool")
    if not all(math.isfinite(float(row["score"])) for row in ranking):
        raise ValueError("non-finite proposal score")
    ordered = sorted(ranking, key=lambda row: (-float(row["score"]), row["candidate_id"]))
    proposed = _batch(ordered, entries, budget)
    reference = record["diagnostics"]
    return {
        "reference_protocol": NAME,
        "reference_batch": reference, "proposed_batch": proposed,
        "selected_overlap": len(set(record["selected_ids"]) & {row["candidate_id"] for row in ordered[:budget]}),
        "estimated_core_hours_delta": proposed["estimated_batch_core_hours"] - reference["estimated_batch_core_hours"],
        "mean_novelty_delta": proposed["mean_novelty"] - reference["mean_novelty"],
        "mean_sparsity_delta": proposed["mean_sparsity"] - reference["mean_sparsity"],
        "limits": "Counterfactual feature/cost diagnostics only; no MAE gain or causal effect is measured.",
    }
