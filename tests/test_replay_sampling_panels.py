import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from analysis.sampling.replay_sampling_panels import summarize, verified_gt


def point(variant: Any, seed: Any, index: Any, cost: Any, mae: Any) -> Any:
    return {
        "space": "autode",
        "variant": variant,
        "seed": seed,
        "round_index": index,
        "cost": cost,
        "mae": mae,
    }


def test_unfinished_tracks_stay_visible_without_extrapolation() -> None:
    points = [
        point("random", seed, index, 10 * (index + 1), 5 - index + seed)
        for seed in range(5)
        for index in range(3)
    ]
    points += [
        point("reviewed", seed, index, 10 * (index + 1), 4 - index + seed)
        for seed in range(5)
        for index in range(2 if seed == 0 else 3)
    ]
    means, residuals = summarize(points)
    tail = next(r for r in means if r["variant"] == "reviewed" and r["cost"] == 30)
    assert tail["n_seeds"] == 4
    assert 0 not in tail["seeds"]
    assert next(r for r in residuals if r["cost"] == 30)["n_seeds"] == 4


def test_residual_sd_is_paired_not_independent_error_propagation() -> None:
    points = [
        point(v, s, i, 10 * (i + 1), 5 + s - i - (v == "reviewed"))
        for v in ("random", "reviewed")
        for s in range(5)
        for i in range(2)
    ]
    _, residuals = summarize(points)
    assert all(r["mean"] == -1 for r in residuals)
    assert all(r["sd"] == 0 for r in residuals)


def test_budget_alignment_uses_last_affordable_checkpoint() -> None:
    points = [
        point("random", 0, 0, 10, 6),
        point("random", 0, 1, 30, 4),
        point("reviewed", 0, 0, 10, 5),
        point("reviewed", 0, 1, 20, 2),
        point("reviewed", 0, 2, 30, 1),
    ]
    means, residuals = summarize(points)
    at_twenty = next(r for r in residuals if r["cost"] == 20)
    assert at_twenty["mean"] == -4
    assert at_twenty["sd"] is None
    assert all(r["sd"] is None for r in means)


def test_residual_uses_only_seed_pairs_with_both_methods_observed() -> None:
    points = [
        point("random", 0, 0, 10, 6),
        point("random", 0, 1, 30, 4),
        point("reviewed", 1, 0, 10, 5),
        point("reviewed", 1, 1, 30, 2),
    ]
    means, residuals = summarize(points)
    assert means
    assert not residuals


def test_literature_suite_without_random_uses_predictive_as_paired_reference() -> None:
    points = [
        point(v, s, i, 10 * (i + 1), 5 + s - i - (v == "ucb"))
        for v in ("predictive-uncertainty", "ucb")
        for s in range(5)
        for i in range(2)
    ]
    means, residuals = summarize(points)
    assert {row["variant"] for row in means} == {"predictive-uncertainty", "ucb"}
    assert all(
        row["variant"] == "ucb" and row["mean"] == -1 and row["sd"] == 0 for row in residuals
    )


def test_invalid_or_gapped_trajectory_is_rejected() -> None:
    with pytest.raises(ValueError, match="noncontiguous"):
        summarize([point("random", 0, 1, 10, 5)])
    with pytest.raises(ValueError, match="decreasing"):
        summarize([point("random", 0, 0, 20, 5), point("random", 0, 1, 10, 4)])


def test_final_gt_round_does_not_require_an_auxiliary_validation_output(tmp_path: Path) -> None:
    folder = tmp_path / "round-0039"
    (folder / "result").mkdir(parents=True)
    evaluation = {
        "mae": 0.4,
        "n": 123,
        "role": "evaluation-only",
        "unit": "kcal/mol",
        "ground_truth_sha256": "fixed",
    }
    path = folder / "result/evaluation.json"
    path.write_text(json.dumps(evaluation))
    (folder / "result/worker-report.json").write_text(
        json.dumps(
            {
                "source_revision": "pinned",
                "outputs": {"evaluation.json": hashlib.sha256(path.read_bytes()).hexdigest()},
            }
        )
    )
    (folder / "evaluation-summary.json").write_text(
        json.dumps({**evaluation, "seed": 0, "round_index": 39})
    )
    experiment = {"source_revision": "pinned", "evaluation_sha256": "fixed"}
    assert verified_gt(folder, experiment, 0)["gt_mae"] == 0.4
    path.write_text(json.dumps({**evaluation, "mae": 0.01}))
    with pytest.raises(ValueError, match="output hash mismatch"):
        verified_gt(folder, experiment, 0)
