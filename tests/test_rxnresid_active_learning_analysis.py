from __future__ import annotations

from pathlib import Path

from analysis.sampling.rxnresid_active_learning_analysis import _correct_trajectory


def test_correct_trajectory_uses_materialized_hours_and_keeps_missing_pairs() -> None:
    curve = [
        {
            "trajectory_id": "autode__random__seed-00",
            "round_index": "0",
            "new_pair_count": "2",
            "total_selected_pairs": "2",
            "round_sampling_cost_core_hours": "14400",
            "cumulative_sampling_cost_core_hours": "14400",
        }
    ]
    decisions = [
        {
            "round_index": "0",
            "pair_id": "ene-a+diene-a",
            "resource_cost_raw": "3600",
            "materialized_sampling_cost_core_hours": "3600",
            "cost_source_column": "ts_compute_seconds_sum",
            "cost_unit": "materialized_core_hours",
        },
        {
            "round_index": "0",
            "pair_id": "ene-b+diene-b",
            "resource_cost_raw": "",
            "materialized_sampling_cost_core_hours": "0",
            "cost_source_column": "ts_compute_seconds_sum",
            "cost_unit": "materialized_core_hours",
        },
    ]

    corrected_curve, corrected_decisions, audit = _correct_trajectory(
        curve_rows=curve,
        decision_rows=decisions,
        costs={"ene-a+diene-a": 1.0},
        missing_by_pair={"ene-a+diene-a": False},
        seconds_by_pair={"ene-a+diene-a": 3600.0},
        resource_path=Path("resources.csv"),
    )

    assert corrected_curve[0]["round_sampling_cost_core_hours"] == "1"
    assert corrected_curve[0]["cumulative_sampling_cost_core_hours"] == "1"
    assert corrected_decisions[0]["materialized_sampling_cost_core_hours"] == "1"
    assert corrected_decisions[1]["materialized_sampling_cost_core_hours"] == "0"
    assert corrected_decisions[1]["cost_was_missing"] == "true"
    assert audit["unknown_resource_pairs"] == 1
    assert audit["corrected_total_seconds_equivalent"] == 3600.0
