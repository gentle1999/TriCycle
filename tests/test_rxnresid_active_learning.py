from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tricycle_reaction_db.pools import export_database_collections
from tricycle_reaction_db.substrate_adaptation import (
    _same_substrate_graph,
    _substrate_graph,
    adapt_database_export,
)
from tricycle_sampling.active_learning import (
    ExperimentSpec,
    _bohr_request,
    _worker_launcher,
    build_pair_pool,
    collect_results,
    normalize_collection,
    parse_materialized_cost,
)
from tricycle_sampling.trajectory_worker import _parse_prediction_output

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


class DatabaseExportAdaptationTests(unittest.TestCase):
    def test_explicit_h_ring_stereo_survives_map_and_atom_order_changes(self) -> None:
        from rdkit import Chem

        source = "[H:11]/[C:12]1=[C:13](/[H:14])[C:15]([H:16])([H:17])[C:18]([H:19])([H:20])[C:21]([H:22])([H:23])[C:24]([H:25])([H:26])[C:27]([H:28])([H:29])[C:30]([H:31])([H:32])[C:33]1([H:34])[H:35]"
        params: Any = Chem.SmilesParserParams()
        params.removeHs = False
        molecule = Chem.MolFromSmiles(source, params)
        for index, atom in enumerate(molecule.GetAtoms()):
            atom.SetAtomMapNum(molecule.GetNumAtoms() - index)
        reordered = Chem.RenumberAtoms(molecule, list(reversed(range(molecule.GetNumAtoms()))))
        rendered = Chem.MolToSmiles(
            reordered, canonical=False, isomericSmiles=True, allHsExplicit=True
        )
        graph = _substrate_graph(source)
        self.assertTrue(_same_substrate_graph(graph, _substrate_graph(rendered)))
        self.assertTrue(_same_substrate_graph(graph, _substrate_graph("C1=C/CCCCCCC/1")))
        self.assertFalse(_same_substrate_graph(graph, _substrate_graph("C1=C\\CCCCCCC/1")))
        self.assertFalse(_same_substrate_graph(graph, _substrate_graph("C1=CCCCCCCC1")))

    def test_does_not_merge_enantiomers_isotopes_or_charge_states(self) -> None:
        for left, right in [
            ("N[C@H](C)O", "N[C@@H](C)O"),
            ("N[C@H](C)O", "NC(C)O"),
            ("[13CH2]=C", "C=C"),
            ("[NH4+]", "N"),
        ]:
            self.assertFalse(_same_substrate_graph(_substrate_graph(left), _substrate_graph(right)))
            self.assertFalse(_same_substrate_graph(_substrate_graph(right), _substrate_graph(left)))

    def test_aliases_are_rebuilt_from_source_graphs_instead_of_legacy_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, substrates = root / "native.csv", root / "substrates.csv"
            _write(
                raw,
                ["mapped_reaction_id", "mapped_reaction_smiles"],
                [
                    {
                        "mapped_reaction_id": "one",
                        "mapped_reaction_smiles": "C1=C/CCCCCCC/1.C=CC=C>>C1CC=CCC1",
                    }
                ],
            )
            _write(
                substrates,
                ["ene_id", "ene_smiles", "diene_id", "diene_smiles"],
                [
                    {
                        "ene_id": "1",
                        "ene_smiles": "C1=C\\CCCCCCC/1",
                        "diene_id": "2",
                        "diene_smiles": "C=CC=C",
                    }
                ],
            )
            aliases = root / "source-alias-dictionary.json"
            aliases.write_text(json.dumps({"ene": {"C1=C\\CCCCCCC/1": ["1"]}, "diene": {}}))
            (root / "corrected-summary.json").write_text(json.dumps({"project_id": "proxy"}))
            (root / "raw").mkdir()
            (root / "raw/details-000000.json").write_text(
                json.dumps(
                    {
                        "project_id": "proxy",
                        "data": {
                            "MappedReactionQueryService": {
                                "r0": {
                                    "id": "one",
                                    "participants": [
                                        {"id": "part", "mapped_smiles": "C1=C/CCCCCCC/1"}
                                    ],
                                    "nodes": [
                                        {
                                            "role": "reactant",
                                            "geometries": [
                                                {
                                                    "mapped_reaction_participant_id": "part",
                                                    "calculations": [
                                                        {
                                                            "artifact_file_id": "artifact-one",
                                                            "original_filename": "1_0.ene.log",
                                                        }
                                                    ],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            }
                        },
                    }
                )
            )
            result = adapt_database_export(
                raw, substrates, root / "out", source_aliases=aliases, project_id="proxy"
            )
            self.assertEqual(result["match_status_counts"], {"matched": 1})
            row = list(csv.DictReader((root / "out/reaction-collection.csv").open()))[0]
            self.assertEqual(row["substrate_match_method"], "project-reactant-graph-alias")
            self.assertTrue(result["alias_binding_evidence_sha256"])

    def test_preserves_profiles_and_matches_maps_order_and_oxide_representations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            substrates = root / "substrates.csv"
            _write(
                substrates,
                ["ene_id", "ene_smiles", "diene_id", "diene_smiles"],
                [
                    {"ene_id": "1", "ene_smiles": "C=C", "diene_id": "2", "diene_smiles": "C=CC=C"},
                    {
                        "ene_id": "3",
                        "ene_smiles": "C=CP(=O)(C)C",
                        "diene_id": "2",
                        "diene_smiles": "C=CC=C",
                    },
                ],
            )
            raw = root / "native.csv"
            columns = [
                "mapped_reaction_id",
                "mapped_reaction_smiles",
                "activation_gibbs_free_energy_kcal_mol",
                "level_of_theory",
                "products_running_time_seconds",
            ]
            source = [
                {
                    "mapped_reaction_id": "one",
                    "mapped_reaction_smiles": "[CH2:7]=[CH:3][CH:6]=[CH2:8].[CH2:2]=[CH2:1]>>C1CC=CCC1",
                    "activation_gibbs_free_energy_kcal_mol": "12.123456789",
                    "level_of_theory": "first",
                    "products_running_time_seconds": "",
                },
                {
                    "mapped_reaction_id": "one",
                    "mapped_reaction_smiles": "C=C.C=CC=C>>C1CC=CCC1",
                    "activation_gibbs_free_energy_kcal_mol": "99.25",
                    "level_of_theory": "second",
                    "products_running_time_seconds": "0",
                },
                {
                    "mapped_reaction_id": "oxide",
                    "mapped_reaction_smiles": "C=CC=C.C=C[P+](C)(C)[O-]>>C1CC=CCC1",
                    "activation_gibbs_free_energy_kcal_mol": "",
                    "level_of_theory": "third",
                    "products_running_time_seconds": "",
                },
                {
                    "mapped_reaction_id": "unknown",
                    "mapped_reaction_smiles": "N.C=CC=C>>C1CC=CCC1",
                    "activation_gibbs_free_energy_kcal_mol": "3",
                    "level_of_theory": "fourth",
                    "products_running_time_seconds": "",
                },
            ]
            _write(raw, columns, source)
            result = adapt_database_export(raw, substrates, root / "adapted")
            rows = list(csv.DictReader((root / "adapted/reaction-collection.csv").open()))
            restored = sorted(rows, key=lambda row: int(row["export_row_index"]))
            self.assertEqual([{key: row[key] for key in columns} for row in restored], source)
            self.assertEqual(restored[0]["pair_id"], "000000000001+000000000002")
            self.assertEqual(restored[2]["pair_id"], "000000000003+000000000002")
            self.assertEqual(result["match_status_counts"], {"matched": 3, "unmatched": 1})
            self.assertEqual(result["row_count"], 4)

    def test_keeps_ambiguous_structures_and_rejects_cross_project_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, substrates = root / "native.csv", root / "substrates.csv"
            _write(
                raw,
                ["mapped_reaction_id", "mapped_reaction_smiles"],
                [{"mapped_reaction_id": "one", "mapped_reaction_smiles": "C=C.C=CC=C>>C1CC=CCC1"}],
            )
            _write(
                substrates,
                ["ene_id", "ene_smiles", "diene_id", "diene_smiles"],
                [
                    {
                        "ene_id": str(ident),
                        "ene_smiles": "C=C",
                        "diene_id": "2",
                        "diene_smiles": "C=CC=C",
                    }
                    for ident in (1, 3)
                ],
            )
            result = adapt_database_export(raw, substrates, root / "out")
            self.assertEqual(result["match_status_counts"], {"ambiguous": 1})
            aliases = root / "source-alias-dictionary.json"
            aliases.write_text(json.dumps({"ene": {}, "diene": {}}))
            (root / "corrected-summary.json").write_text(json.dumps({"project_id": "ground-truth"}))
            with self.assertRaisesRegex(ValueError, "requested database project"):
                adapt_database_export(
                    raw, substrates, root / "out", source_aliases=aliases, project_id="proxy"
                )

    def test_export_dry_run_does_not_construct_database_adapter(self) -> None:
        with patch(
            "tricycle_reaction_db.McpReactionDatabaseAdapter.from_env",
            side_effect=AssertionError("credentials must not be read"),
        ):
            result = export_database_collections(Path("configs/database-sampling-pools.toml"))
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["tool_name"], "export_mapped_reaction_thermodynamics_csv")


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
        assert isinstance(parameters, dict)
        self.assertEqual(
            parameters["backward_files"],
            ["outputs/autode__random__seed-00/"],
        )

    def test_worker_launcher_reuses_sar_environment_without_install_fallback(self) -> None:
        launcher = _worker_launcher()
        self.assertIn("$PWD/tricycle-sar/src", launcher)
        self.assertIn("tricycle_sar.sampling_runtime initialize", launcher)
        self.assertIn('exec "$model_python"', launcher)
        self.assertNotIn("pip install", launcher)
        self.assertNotIn("runtime_ready", launcher)

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
            from tricycle_sampling.trajectory_worker import _read_csv

            expected = _read_csv(prediction_path)
            for index, row in enumerate(expected):
                row["path_id"] = f"path-{index}"
            _write(prediction_path, list(expected[0]), expected)
            mae, scores, _ = _parse_prediction_output(
                prediction_path,
                expected_rows=3,
                expected_pairs={"ene-0+diene-0", "ene-1+diene-0"},
                expected=expected,
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
