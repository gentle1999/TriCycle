import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from tricycle_sampling.replay.validation_monitor import accepted_round, collect


def write(path: Any, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def campaign(tmp_path: Path) -> Any:
    experiment_root = tmp_path / "experiment"
    experiment = {
        "source_revision": "pinned-source",
        "evaluation_sha256": "pinned-gt",
        "validation_panels": {"0": ["a", "b"]},
    }
    write(experiment_root / "experiment.json", experiment)
    write(
        tmp_path / "sampling-plan.json",
        {"experiments": [{"project": "test", "variant": "random", "root": str(experiment_root)}]},
    )
    write(experiment_root / "seed-00/state.json", {"seed": 0, "round": 2, "phase": "monitor"})

    def add_round(index: Any, mae: Any = 2.0) -> Any:
        directory = experiment_root / "seed-00" / f"round-{index:04d}"
        validation = {
            "role": "sampled-pair-holdout",
            "validation_policy": "fixed-initial-pair-panel",
            "scope": "auxiliary model trained from scratch; not full-model validation",
            "validation_pairs": ["a", "b"],
            "training_pairs": ["c"],
            "panel_pair_count": 2,
            "checkpoint_sha256": "aux-checkpoint",
            "n": 3,
            "mae": mae,
            "unit": "kcal/mol",
        }
        evaluation = {
            "role": "evaluation-only",
            "ground_truth_sha256": "pinned-gt",
            "n": 17,
            "mae": 4.0,
            "unit": "kcal/mol",
        }
        write(directory / "result/model-metrics.json", validation)
        write(directory / "result/evaluation.json", evaluation)
        outputs = {
            name: hashlib.sha256((directory / "result" / name).read_bytes()).hexdigest()
            for name in ("model-metrics.json", "evaluation.json")
        }
        outputs["validation-fit/model/checkpoint.pt"] = "aux-checkpoint"
        write(
            directory / "result/worker-report.json",
            {"source_revision": "pinned-source", "outputs": outputs},
        )
        write(
            directory / "evaluation-summary.json",
            {
                **evaluation,
                "seed": 0,
                "round_index": index,
                "cumulative_sampling_cost_core_hours": 10 * (index + 1),
            },
        )
        return directory

    return tmp_path, experiment_root, experiment, add_round


def alter_validated_metric(directory: Any, changes: Any) -> None:
    path = directory / "result/model-metrics.json"
    value = json.loads(path.read_text())
    value.update(changes)
    write(path, value)
    report_path = directory / "result/worker-report.json"
    report = json.loads(report_path.read_text())
    report["outputs"]["model-metrics.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
    write(report_path, report)


def test_latest_completed_round_and_delta_not_running_round(campaign: Any) -> None:
    root, experiment_root, _, add_round = campaign
    add_round(0, 3.0)
    add_round(1, 2.0)
    # A partial download without the accepted evaluation summary is excluded.
    write(experiment_root / "seed-00/round-0002/result/model-metrics.json", {"mae": 0.01})
    report = collect(root)
    latest = report["latest"][0]
    assert latest["current_round"] == 2
    assert latest["completed_round"] == 1
    assert latest["validation_mae"] == 2.0
    assert latest["validation_mae_delta"] == -1.0
    assert latest["gt_mae"] == 4.0
    assert len(report["history"]) == 2
    assert not report["errors"]


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"validation_pairs": ["a", "c"]}, "panel mismatch"),
        ({"training_pairs": ["a", "c"]}, "leaked"),
        ({"mae": float("nan")}, "invalid MAE"),
        ({"checkpoint_sha256": "main-checkpoint"}, "auxiliary checkpoint"),
    ],
)
def test_reject_invalid_scientific_provenance(campaign: Any, changes: Any, match: Any) -> None:
    _, _, experiment, add_round = campaign
    directory = add_round(0)
    alter_validated_metric(directory, changes)
    with pytest.raises(ValueError, match=match):
        accepted_round(directory, experiment, 0)


def test_corrupt_latest_falls_back_with_visible_error(campaign: Any) -> None:
    root, _, _, add_round = campaign
    add_round(0, 3.0)
    directory = add_round(1)
    write(directory / "result/model-metrics.json", {"mae": 0.0})
    report = collect(root)
    assert report["latest"][0]["completed_round"] == 0
    assert "hash mismatch" in report["errors"][0]["error"]


def test_missing_metric_is_null_not_zero(campaign: Any) -> None:
    root, _, _, _ = campaign
    report = collect(root)
    assert report["latest"][0]["validation_mae"] is None
    assert report["groups"][0]["validation_mae_mean"] is None
    assert report["groups"][0]["available_seed_count"] == 0
