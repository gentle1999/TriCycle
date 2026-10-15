"""Core-hour objectives and cost evidence; evaluation labels never enter decisions."""

from __future__ import annotations

import math
import statistics
from typing import Any

NAME = "autonomous-core-hours-mae-v1"
OBJECTIVE = "minimize-ground-truth-mae-at-core-hour-budget"


def budgets(value: Any) -> list[float]:
    if not isinstance(value, list) or not value:
        raise ValueError("core-hour protocol requires evaluation_budgets_core_hours")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in value):
        raise ValueError("core-hour budgets must be numbers")
    result = [float(x) for x in value]
    if any(not math.isfinite(x) or x <= 0 for x in result) or result != sorted(set(result)):
        raise ValueError("core-hour budgets must be positive, finite, strictly increasing")
    return result


def objective(protocol: dict[str, Any] | None) -> str:
    return OBJECTIVE if protocol and protocol.get("name") == NAME else "minimize-generalization-mae"


def cost_evidence(
    observations: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    selected_ids: list[str],
    spent: float,
    milestones: list[float],
) -> dict[str, Any]:
    """Calibrate units from revealed timings only, without access to oracle files."""
    milestones = budgets(milestones)
    if not math.isfinite(spent) or spent < 0:
        raise ValueError("invalid realized core-hour expenditure")
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("duplicate revealed pair")
    ids = [r["candidate_id"] for r in observations]
    if len(ids) != len(set(ids)) or set(ids) != set(selected_ids):
        raise ValueError("cost observations must cover exactly the revealed pairs")
    if set(selected_ids) & {r["candidate_id"] for r in candidates}:
        raise ValueError("cost estimation candidates overlap revealed pairs")
    ratios = []
    for row in observations:
        cost, proxy = float(row["realized_core_hours"]), float(row["structural_cost_proxy"])
        if not math.isfinite(cost) or cost < 0 or not math.isfinite(proxy) or proxy <= 0:
            raise ValueError("invalid revealed cost observation")
        if not row.get("cost_was_missing", False) and cost > 0:
            ratios.append(cost / proxy)
    if not math.isclose(
        sum(float(r["realized_core_hours"]) for r in observations), spent, rel_tol=0, abs_tol=1e-6
    ):
        raise ValueError("revealed timing total differs from realized expenditure")
    if not ratios:
        raise ValueError("no measured positive revealed timing for core-hour calibration")
    scale = statistics.median(ratios)
    for candidate in candidates:
        p = candidate["payload"]
        proxy = float(p["product_count"]) * float(p["mean_product_atom_count"])
        estimate = proxy * scale
        if not math.isfinite(estimate) or estimate <= 0:
            raise ValueError("invalid candidate core-hour estimate")
        p["estimated_core_hours"] = estimate
    future = [b for b in milestones if b > spent]
    return {
        "unit": "core_hours",
        "spent_core_hours": spent,
        "evaluation_budgets_core_hours": milestones,
        "next_budget_core_hours": future[0] if future else milestones[-1],
        "remaining_to_next_budget_core_hours": max(
            0.0, (future[0] if future else milestones[-1]) - spent
        ),
        "calibration": {
            "method": "median revealed timing / structural cost proxy",
            "core_hours_per_proxy_unit": scale,
            "measured_pairs": len(ratios),
            "revealed_pairs": len(observations),
            "missing_cost_pairs": sum(bool(r.get("cost_was_missing")) for r in observations),
        },
        "limits": [
            "Estimated costs are predictions, not unselected historical timings.",
            "Realized historical accounting includes explicitly materialized missing costs.",
            "Milestones define evaluation budgets, not a guaranteed hard spending cap.",
            "A checkpoint exceeding a milestone is excluded from its evaluation.",
            "SAR training and agent overhead are reported separately.",
        ],
    }


def ranking_diagnostics(
    ranking: list[dict[str, Any]], count: int, candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Expose tie effects without prescribing weights, diversity, or a new formula."""
    if not 0 < count <= len(ranking):
        raise ValueError("invalid diagnostic batch size")
    ranked = sorted(ranking, key=lambda r: (-float(r["score"]), r["candidate_id"]))
    selected = ranked[:count]
    cutoff = float(selected[-1]["score"])
    payloads = {c["candidate_id"]: c["payload"] for c in candidates}
    costs = [float(payloads[r["candidate_id"]]["estimated_core_hours"]) for r in selected]
    return {
        "unique_scores": len({float(r["score"]) for r in ranked}),
        "cutoff_tied_candidates": sum(float(r["score"]) == cutoff for r in ranked),
        "selected_at_cutoff": sum(float(r["score"]) == cutoff for r in selected),
        "tie_break": "candidate_id ascending",
        "selected_unique_ene": len({r["candidate_id"].split("+")[0] for r in selected}),
        "selected_unique_diene": len({r["candidate_id"].split("+")[-1] for r in selected}),
        "estimated_batch_core_hours": sum(costs),
        "limits": "Diagnostics only; no automatic diversity or cost penalty; no measured unselected cost.",
    }


def aggregate(
    points: list[dict[str, Any]], variants: list[str], milestones: list[float]
) -> dict[str, Any]:
    """Evaluate latest affordable checkpoints, using the same five seeds at every budget."""
    milestones = budgets(milestones)
    tracks: dict[tuple[str, int], list[dict[str, Any]]] = {
        (v, s): [] for v in variants for s in range(5)
    }
    scope = None
    for p in points:
        key = (p["variant"], p["seed"])
        if key not in tracks:
            raise ValueError("unexpected variant or seed")
        current_scope = (p["ground_truth_sha256"], p["n"], p["unit"], p["role"])
        if scope is None:
            scope = current_scope
        if current_scope != scope or p["role"] != "evaluation-only":
            raise ValueError("evaluation scope mismatch")
        if any(
            not math.isfinite(float(p[k])) or float(p[k]) < 0
            for k in ("mae", "rmse", "cumulative_sampling_cost_core_hours")
        ):
            raise ValueError("nonfinite or negative cost/metric")
        tracks[key].append(p)
    for ts in tracks.values():
        ts.sort(key=lambda p: p["round_index"])
        if [p["round_index"] for p in ts] != list(range(len(ts))):
            raise ValueError("noncontiguous trajectory")
        if any(
            b["cumulative_sampling_cost_core_hours"] < a["cumulative_sampling_cost_core_hours"]
            for a, b in zip(ts, ts[1:], strict=False)
        ):
            raise ValueError("decreasing cumulative core-hours")
    summaries, paired = [], []
    assessment: dict[str, Any] | None = None
    if "intelligent" in variants:
        from tricycle_sampling.protocols import LITERATURE_BASELINE_STRATEGIES

        assessment = {
            "schema": "tricycle.intelligent-comparison/v1",
            "strategy": "intelligent",
            "baselines": list(LITERATURE_BASELINE_STRATEGIES),
            "budgets": [],
            "criterion": "Lower matched-five-seed mean MAE than every baseline at every preregistered core-hour milestone.",
            "scope": "Descriptive performance criterion; not a statistical significance claim; never acquisition feedback.",
        }
    for budget in milestones:
        values: dict[str, list[dict[str, Any]]] = {}
        for variant in variants:
            rows = []
            for seed in range(5):
                ts = tracks[(variant, seed)]
                # No carrying an unfinished trajectory beyond its observed coverage.
                if not ts or round(ts[-1]["cumulative_sampling_cost_core_hours"], 6) < budget:
                    break
                affordable = [
                    p for p in ts if round(p["cumulative_sampling_cost_core_hours"], 6) <= budget
                ]
                if not affordable:
                    break
                rows.append(affordable[-1])
            if len(rows) != 5:
                continue
            values[variant] = rows
            maes = [float(p["mae"]) for p in rows]
            summaries.append(
                {
                    "variant": variant,
                    "budget_core_hours": budget,
                    "n_seeds": 5,
                    "mae_mean": statistics.mean(maes),
                    "mae_sd": statistics.stdev(maes),
                    "rmse_mean": statistics.mean(float(p["rmse"]) for p in rows),
                    "round_indices": [p["round_index"] for p in rows],
                }
            )
        reference = "autonomous" if "autonomous" in variants else variants[0] if variants else None
        if reference in values:
            for variant, rows in values.items():
                if variant == reference:
                    continue
                ds = [
                    float(p["mae"]) - float(q["mae"])
                    for p, q in zip(rows, values[reference], strict=True)
                ]
                paired.append(
                    {
                        "variant": variant,
                        "reference": reference,
                        "budget_core_hours": budget,
                        "n_seeds": 5,
                        "seed_deltas": ds,
                        "mae_delta_mean": statistics.mean(ds),
                        "mae_delta_sd": statistics.stdev(ds),
                    }
                )
        if assessment is not None:
            required = ["intelligent", *assessment["baselines"]]
            missing = [variant for variant in required if variant not in values]
            comparisons = []
            if not missing:
                for baseline in assessment["baselines"]:
                    deltas = [
                        float(p["mae"]) - float(q["mae"])
                        for p, q in zip(values["intelligent"], values[baseline], strict=True)
                    ]
                    comparisons.append(
                        {
                            "baseline": baseline,
                            "n_seeds": 5,
                            "seed_deltas": deltas,
                            "mae_delta_mean": statistics.mean(deltas),
                            "mae_delta_sd": statistics.stdev(deltas),
                            "lower_mean_mae": statistics.mean(deltas) < 0,
                        }
                    )
            assessment["budgets"].append(
                {
                    "budget_core_hours": budget,
                    "missing_coverage": missing,
                    "comparisons": comparisons,
                    "status": "pending"
                    if missing
                    else "passed"
                    if all(row["lower_mean_mae"] for row in comparisons)
                    else "not-met",
                }
            )
    if assessment is not None:
        statuses = [row["status"] for row in assessment["budgets"]]
        assessment["status"] = (
            "pending"
            if "pending" in statuses
            else "passed"
            if all(status == "passed" for status in statuses)
            else "not-met"
        )
    return {
        **({"intelligent_comparison": assessment} if assessment is not None else {}),
        "budget_axis": "core_hours",
        "evaluation_budgets_core_hours": milestones,
        "summaries": summaries,
        "paired_differences": paired,
        "limits": "Five matched seeds only; latest affordable checkpoint; no interpolation, extrapolation, or best-MAE selection.",
    }
