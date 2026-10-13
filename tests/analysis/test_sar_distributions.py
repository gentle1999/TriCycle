from __future__ import annotations

import pytest

from analysis.sar.distributions import activation_distribution_data


def test_component_identifiers_are_opaque_strings() -> None:
    data = activation_distribution_data(
        [
            {"path_id": "a", "ene_id": "001", "diene_id": "partner", "G": "1"},
            {"path_id": "b", "ene_id": "1", "diene_id": "partner", "G": "3"},
        ],
        "G",
    )
    assert data["summary"]["groups"] == 2
    assert data["within_group_residuals"] == [0, 0]


def test_unequal_group_sizes_keep_weightings_and_singletons_distinct() -> None:
    rows = [
        {"path_id": "a", "ene_id": "001", "diene_id": "2", "G": "1"},
        {"path_id": "b", "ene_id": "001", "diene_id": "2", "G": "3"},
        {"path_id": "c", "ene_id": "3", "diene_id": "2", "G": "10"},
    ]
    data = activation_distribution_data(rows, "G")
    assert data["within_group_residuals"] == [-1, 1, 0]
    assert data["multi_path_residuals"] == [-1, 1]
    assert data["group_mean_residuals"] == [-4, 4]
    summary = data["summary"]
    assert summary["singleton_groups"] == 1
    assert summary["path_weighted_mean"] == pytest.approx(14 / 3)
    assert summary["equal_group_mean"] == 6
    assert summary["equal_group_mean_within_variance"] == 0.5
    assert summary["equal_group_between_variance"] == 16
    decomposition = summary["path_weighted_variance_decomposition"]
    assert decomposition["within"] == pytest.approx(2 / 3)
    assert decomposition["between"] == pytest.approx(128 / 9)
    assert decomposition["total"] == pytest.approx(134 / 9)


def test_all_singleton_groups_have_no_multi_path_distribution() -> None:
    rows = [{"path_id": "a", "ene_id": "1", "diene_id": "2", "G": "5"}]
    data = activation_distribution_data(rows, "G")
    assert data["multi_path_residuals"] == []
    assert data["within_group_residuals"] == [0]
    assert data["group_mean_residuals"] == [0]
    assert data["summary"]["path_weighted_variance_decomposition"]["between_fraction"] is None
