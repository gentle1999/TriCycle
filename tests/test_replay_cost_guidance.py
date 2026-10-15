from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from tricycle_sampling.acquisition import evaluate_numeric_expression
from tricycle_sampling.replay import core_hours as core
from tricycle_sampling.replay import cost_guidance as guidance
from tricycle_sampling.replay import mae as replay_mae
from tricycle_sampling.replay import suite as replay_mae_suite

from tests.test_replay_mae import MaeWorkerTests


def candidates() -> list[dict]:
    return [
        {
            "candidate_id": cid,
            "payload": {
                "sampled_novelty": novelty,
                "pool_local_density": density,
                "product_count": 1,
                "mean_product_atom_count": workload,
                "estimated_core_hours": workload * 2,
            },
        }
        for cid, novelty, density, workload in (
            ("a+1", 0.6, 0.4, 10),
            ("b+2", 1.0, 0.0, 100),
            ("c+3", 0.6, 0.4, 10),
            ("d+4", 0.2, 0.8, 20),
        )
    ]


def test_reference_uses_the_original_median_and_does_not_use_oracle_fields() -> None:
    pool = candidates()
    record = guidance.build(pool, 2)
    assert record["context"]["cost_proxy_median"] == 15
    assert record["selected_ids"] == ["a+1", "c+3"]
    assert record["diagnostics"]["estimated_batch_core_hours"] == 40
    scores = {row["candidate_id"]: row["score"] for row in record["ranking"]}
    assert scores["b+2"] == pytest.approx(0.15)
    for c in pool:
        score = evaluate_numeric_expression(
            guidance.EXPRESSION,
            candidate=c["payload"],
            context={"coverage_cost": record["context"]},
        )
        assert score == pytest.approx(scores[c["candidate_id"]])
    for c in pool:
        c["payload"].update({"target": 99999, "future_true_cost": 99999, "gt_mae": 99999})
    assert guidance.build(pool[::-1], 2) == record
    assert "99999" not in json.dumps(guidance.compact(record))


def test_proposal_comparison_keeps_agent_ranking_and_rejects_drift() -> None:
    pool = candidates()
    record = guidance.build(pool, 2)
    ranking = [{"candidate_id": c["candidate_id"], "score": i} for i, c in enumerate(pool)]
    before = copy.deepcopy(ranking)
    comparison = guidance.compare(record, ranking, pool, 2)
    assert comparison["selected_overlap"] == 1
    assert comparison["estimated_core_hours_delta"] == 20
    assert ranking == before
    with pytest.raises(ValueError, match="complete"):
        guidance.compare(record, ranking[:-1], pool, 2)
    pool[0]["payload"]["sampled_novelty"] = 0.1
    with pytest.raises(ValueError, match="differs"):
        guidance.compare(record, ranking, pool, 2)


@pytest.mark.parametrize(
    "key,value",
    [
        ("sampled_novelty", 2),
        ("pool_local_density", -1),
        ("estimated_core_hours", 0),
        ("product_count", float("nan")),
    ],
)
def test_invalid_reference_inputs_block_instead_of_falling_back(key: str, value: float) -> None:
    pool = candidates()
    pool[0]["payload"][key] = value
    with pytest.raises(ValueError):
        guidance.build(pool, 2)


def test_guidance_is_versioned_and_requires_feedback_review_and_core_hours() -> None:
    value = {
        "name": core.NAME,
        "evaluation_budgets_core_hours": [50, 100],
        "feedback": True,
        "review": True,
        "guidance": guidance.NAME,
    }
    settings = replay_mae.settings({"decision_protocol": value})
    assert settings is not None and settings["guidance"] == guidance.NAME
    for changed in (
        {"feedback": False},
        {"review": False},
        {"name": replay_mae.NAME},
        {"guidance": "unknown"},
    ):
        with pytest.raises(ValueError):
            replay_mae.settings({"decision_protocol": {**value, **changed}})
    brief = {
        "decision_protocol": settings,
        "objective": core.OBJECTIVE,
        "core_hour_budget": {"unit": "core_hours"},
        "acquisition_guidance": guidance.compact(guidance.build(candidates(), 2)),
    }
    prompt = replay_mae.prompt(brief)
    assert guidance.EXPRESSION in prompt
    assert "COVERAGE AND COST" in prompt
    assert "does not overwrite your ranking" in prompt
    with pytest.raises(ValueError, match="reference"):
        replay_mae.prompt(
            {key: value for key, value in brief.items() if key != "acquisition_guidance"}
        )


def test_final_suite_has_one_agent_and_keeps_shared_main_inputs(tmp_path: Path) -> None:
    path = Path("configs/dpa-unified-sampling-suite.toml")
    config = tomllib.loads(path.read_text())
    assert [
        name for name in config["sampling_strategies"] if name in replay_mae_suite.AGENT_VARIANTS
    ] == ["intelligent"]
    requests = []

    def prepare(request_path: Path, root: Path) -> Path:
        request = tomllib.loads(request_path.read_text())
        requests.append(request)
        root.mkdir()
        value = {
            "round_count": 40,
            "batch_size": 40,
            "training": {"epochs": 300},
            "source_revision": "fixed",
            "evaluation_sha256": "gt",
            "validation_panels": {},
            "private_hashes": {"pair-pool.csv": "pool"},
            "assets_hashes": {"worker.py": "worker"},
            "sampling_experiment": "tricycle.matched-sampling/v1",
            "fixed_seed": 42,
        }
        (root / "experiment.json").write_text(json.dumps(value))
        for seed in range(5):
            folder = root / f"seed-{seed:02d}"
            folder.mkdir()
            (folder / "state.json").write_text(json.dumps({"selected_ids": [f"{seed}:initial"]}))
        return root

    with (
        patch.object(replay_mae_suite, "prepare", side_effect=prepare),
        patch.object(replay_mae_suite, "stage_round"),
        patch.object(replay_mae_suite, "status"),
    ):
        root = replay_mae_suite.prepare_suite(path, tmp_path / "suite")
    plan = json.loads((root / "suite.json").read_text())
    assert len(plan["experiments"]) == 6
    assert plan["maximum_sar_jobs"] == 1200
    assert plan["maximum_concurrent_sar_jobs"] == 30
    assert (
        len({json.dumps(item["shared_protocol"], sort_keys=True) for item in plan["experiments"]})
        == 1
    )
    assert (
        len(
            {
                item["sampling_protocol"]["doi"]
                for item in plan["experiments"]
                if "doi" in item["sampling_protocol"]
            }
        )
        == 5
    )
    assert requests[-1]["decision_protocol"]["guidance"] == guidance.NAME
    assert (
        requests[-1]["decision_protocol"]["feedback"]
        and requests[-1]["decision_protocol"]["review"]
    )
    assert all("guidance" not in item["decision_protocol"] for item in requests[:-1])
    common = [
        {
            key: value
            for key, value in item.items()
            if key not in ("experiment_id", "sampling_strategy", "decision_protocol")
        }
        for item in requests
    ]
    assert all(item == common[0] for item in common)


def test_matched_core_hour_differences_do_not_need_legacy_autonomous() -> None:
    points: list[dict[str, Any]] = [
        {
            "variant": variant,
            "seed": seed,
            "round_index": index,
            "ground_truth_sha256": "gt",
            "n": 10,
            "unit": "kcal/mol",
            "role": "evaluation-only",
            "cumulative_sampling_cost_core_hours": cost,
            "mae": mae,
            "rmse": mae,
        }
        for variant in ("predictive-uncertainty", "intelligent")
        for seed in range(5)
        for index, (cost, mae) in enumerate(((10, 3), (30, 1)))
    ]
    for point in points:
        if point["variant"] == "intelligent":
            point["mae"] -= 0.5
    report = core.aggregate(points, ["predictive-uncertainty", "intelligent"], [20])
    difference = report["paired_differences"][0]
    assert difference["reference"] == "predictive-uncertainty"
    assert difference["seed_deltas"] == [-0.5] * 5


class IntelligentSamplingWorkerTests(MaeWorkerTests):
    protocol_name = core.NAME
    protocol_guidance = guidance.NAME
    protocol_review = True


def test_success_requires_all_baselines_and_all_preregistered_budgets() -> None:
    from tricycle_sampling.protocols import LITERATURE_BASELINE_STRATEGIES

    variants = [*LITERATURE_BASELINE_STRATEGIES, "intelligent"]
    points = [
        {
            "variant": variant,
            "seed": seed,
            "round_index": index,
            "ground_truth_sha256": "gt",
            "n": 10,
            "unit": "kcal/mol",
            "role": "evaluation-only",
            "cumulative_sampling_cost_core_hours": cost,
            "mae": (2 if variant == "intelligent" else 3),
            "rmse": 4,
        }
        for variant in variants
        for seed in range(5)
        for index, cost in enumerate((10, 30, 50))
    ]
    assessment = core.aggregate(points, variants, [20, 40])["intelligent_comparison"]
    assert assessment["status"] == "passed"
    assert len(assessment["budgets"]) == 2
    assert all(len(row["comparisons"]) == 5 for row in assessment["budgets"])
    changed = copy.deepcopy(points)
    for point in changed:
        if point["variant"] == "intelligent" and point["round_index"] == 1:
            point["mae"] = 3
    assessment = core.aggregate(changed, variants, [20, 40])["intelligent_comparison"]
    assert assessment["status"] == "not-met"
    assert assessment["budgets"][0]["status"] == "passed"
    assert assessment["budgets"][1]["status"] == "not-met"
    incomplete = [
        point
        for point in points
        if not (
            point["variant"] == variants[0] and point["seed"] == 4 and point["round_index"] == 2
        )
    ]
    assessment = core.aggregate(incomplete, variants, [20, 40])["intelligent_comparison"]
    assert assessment["status"] == "pending"
    assert assessment["budgets"][1]["comparisons"] == []
    assert assessment["budgets"][1]["missing_coverage"] == [variants[0]]


def test_suite_requires_explicit_strategies_and_replay_rejects_retired_cost_policy(tmp_path: Path) -> None:
    import pytest
    from tricycle_sampling.replay.campaign import prepare
    from tricycle_sampling.replay.suite import prepare_suite

    request = tmp_path / "request.toml"
    request.write_text('suite_id = "test"\n')
    output = tmp_path / "suite"
    with pytest.raises(ValueError, match="sampling_strategies must be supplied explicitly"):
        prepare_suite(request, output)
    request.write_text('[acquisition_policy]\nname = "staged-cost-v2"\n')
    with pytest.raises(ValueError, match="acquisition_policy is no longer supported"):
        prepare(request, output)
    assert not output.exists()
