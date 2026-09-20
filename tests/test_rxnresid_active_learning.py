from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from tools.rxnresid_active_learning import (
    ExperimentSpec,
    _bohr_request,
    _worker_launcher,
    build_pair_pool,
    collect_results,
    normalize_collection,
    parse_materialized_cost,
)
from tools.rxnresid_active_learning_runner import _parse_prediction_output

COLLECTION_FIELDS = [
    "mapped_reaction_id",
    "mapped_reaction_smiles",
    "activation_free_energy_kcal_mol",
    "ene_id",
    "ene_smiles",
    "diene_id",
    "diene_smiles",
]
RESOURCE_FIELDS = ["ene_id", "diene_id", "ts_compute_seconds_sum"]


def _write(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


class RxnResidActiveLearningTests(unittest.TestCase):
    def test_bohr_request_selects_the_explicit_curve_file(self) -> None:
        spec = ExperimentSpec(
            config_path=Path("config.toml"),
            repo_root=Path("."),
            output_root=Path("runs"),
            ground_truth_collection=Path("ground-truth.csv"),
            ground_truth_resources=None,
            projects=(),
            source_directory=None,
            source_repository="https://example.invalid/RxnResid",
            source_revision="revision",
            bohr_project_id="991668",
            bohr_machine_type="1 * NVIDIA T4_16g",
            bohr_image="registry.example.invalid/rxnresid:latest",
            max_run_time=None,
            strategies=(),
            seeds=(),
            pair_batch_size=40,
            round_count=40,
            expected_pair_count=1600,
            uncertainty_aggregation="sum",
            cost_column="ts_compute_seconds_sum",
            cost_unit="materialized_core_hours",
            training_epochs=1,
            training_batch_size_paths=1,
            training_device="auto",
            training_mixed_precision="no",
            persist_predictions=True,
        )
        request = _bohr_request(
            spec,
            trajectory_id="autode__random__seed-00",
            project="autode",
            strategy="random",
            seed=0,
            input_directory=Path("input"),
            trajectory_config=Path("input/trajectories/trajectory.toml"),
        )
        parameters = request["parameters"]
        self.assertEqual(
            parameters["backward_files"],
            ["outputs/autode__random__seed-00/"],
        )

    def test_worker_launcher_is_offline_first_and_retries_install(self) -> None:
        launcher = _worker_launcher()
        self.assertIn("$PWD/rxnresid/src", launcher)
        self.assertIn("install_attempts=5", launcher)
        self.assertIn("--retries 5", launcher)
        self.assertIn("--timeout 120", launcher)
        self.assertIn("sleep \"$install_delay\"", launcher)

    def test_missing_materialized_cost_is_zero_without_unit_conversion(self) -> None:
        self.assertEqual(parse_materialized_cost(""), (0.0, True))
        self.assertEqual(parse_materialized_cost(None), (0.0, True))
        self.assertEqual(parse_materialized_cost("3600"), (3600.0, False))

    def test_pair_pool_retains_pairs_without_project_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gt_path = root / "gt.csv"
            project_path = root / "project.csv"
            resource_path = root / "resources.csv"
            reactions = [
                {
                    "mapped_reaction_id": f"gt-{index}",
                    "mapped_reaction_smiles": "[C:1]=[C:2].[C:3]=[C:4]>>[C:1]1[C:2][C:3]=[C:4]1",
                    "activation_free_energy_kcal_mol": str(10 + index),
                    "ene_id": f"ene-{index}",
                    "ene_smiles": "C=C",
                    "diene_id": "diene-0",
                    "diene_smiles": "C=C-C=C",
                }
                for index in range(2)
            ]
            _write(gt_path, COLLECTION_FIELDS, reactions)
            _write(project_path, COLLECTION_FIELDS, [reactions[0]])
            _write(
                resource_path,
                RESOURCE_FIELDS,
                [
                    {"ene_id": "ene-0", "diene_id": "diene-0", "ts_compute_seconds_sum": "7"},
                    {"ene_id": "ene-1", "diene_id": "diene-0", "ts_compute_seconds_sum": ""},
                ],
            )
            gt = normalize_collection(gt_path, "ground-truth")
            project = normalize_collection(project_path, "project")
            pool = build_pair_pool(
                gt,
                project,
                resource_path,
                cost_column="ts_compute_seconds_sum",
            )
            self.assertEqual(len(pool), 2)
            self.assertEqual(pool[0]["project_label_row_count"], "1")
            self.assertEqual(pool[1]["project_label_row_count"], "0")
            self.assertEqual(pool[1]["materialized_sampling_cost_core_hours"], "0")
            self.assertEqual(pool[1]["cost_was_missing"], "true")

    def test_collect_results_writes_trajectory_and_seed_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            curve = root / "worker" / "outputs" / "curve.csv"
            curve.parent.mkdir(parents=True)
            fields = [
                "trajectory_id",
                "project",
                "strategy",
                "seed",
                "total_selected_pairs",
                "cumulative_sampling_cost_core_hours",
                "full_space_mae_kcal_mol",
            ]
            _write(
                curve,
                fields,
                [
                    {
                        "trajectory_id": "autode__random__seed-00",
                        "project": "autode",
                        "strategy": "random",
                        "seed": "0",
                        "total_selected_pairs": "40",
                        "cumulative_sampling_cost_core_hours": "3",
                        "full_space_mae_kcal_mol": "12.5",
                    }
                ],
            )
            result = collect_results(root)
            self.assertEqual(result["trajectories"], 1)
            self.assertTrue((root / "results" / "curves.csv").is_file())
            self.assertTrue((root / "results" / "curve-summary.csv").is_file())

    def test_uncertainty_scores_are_summed_over_ground_truth_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prediction_path = Path(temporary) / "predictions.csv"
            fields = [
                "ene_id",
                "diene_id",
                "prediction",
                "target",
                "predictive_variance",
                "epistemic_variance",
                "aleatoric_variance",
            ]
            _write(
                prediction_path,
                fields,
                [
                    {
                        "ene_id": "ene-0",
                        "diene_id": "diene-0",
                        "prediction": "1",
                        "target": "2",
                        "predictive_variance": "3",
                        "epistemic_variance": "2",
                        "aleatoric_variance": "1",
                    },
                    {
                        "ene_id": "ene-0",
                        "diene_id": "diene-0",
                        "prediction": "4",
                        "target": "4",
                        "predictive_variance": "5",
                        "epistemic_variance": "4",
                        "aleatoric_variance": "2",
                    },
                    {
                        "ene_id": "ene-1",
                        "diene_id": "diene-0",
                        "prediction": "5",
                        "target": "3",
                        "predictive_variance": "7",
                        "epistemic_variance": "6",
                        "aleatoric_variance": "3",
                    },
                ],
            )
            mae, scores, _ = _parse_prediction_output(
                prediction_path,
                expected_rows=3,
                expected_pairs={"ene-0+diene-0", "ene-1+diene-0"},
            )
            self.assertAlmostEqual(mae, 1.0)
            self.assertEqual(scores["ene-0+diene-0"]["ground_truth_path_count"], 2.0)
            self.assertEqual(scores["ene-0+diene-0"]["predictive_variance"], 8.0)
            self.assertEqual(scores["ene-0+diene-0"]["epistemic_variance"], 6.0)
            self.assertEqual(scores["ene-0+diene-0"]["aleatoric_variance"], 3.0)
            self.assertEqual(scores["ene-1+diene-0"]["ground_truth_path_count"], 1.0)
            self.assertEqual(scores["ene-1+diene-0"]["predictive_variance"], 7.0)


if __name__ == "__main__":
    unittest.main()
