from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from tricycle_core.toml import dump_toml
from tricycle_core.training_protocol import verify_asset_files

from tricycle_sar.benchmark import (
    BenchmarkRequest,
    make_splits,
    prepare,
    read_rows,
    regression_metrics,
    sha256,
    validated_rows,
    write_rows,
)
from tricycle_sar.modelling_benchmark import check, collect, prediction_file, validate_models


class BenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows = [
            {"id": f"p{g}-{path}", "a": str(g), "b": "1", "y": str(g + path), "rxn": "[C:1]>>[C:1]"}
            for g in range(17)
            for path in range(1 + g % 4)
        ]
        self.data = self.root / "data.csv"
        write_rows(self.data, self.rows)
        self.request = BenchmarkRequest(
            self.data, sha256(self.data), "y", "id", ("a", "b"), "rxn", 5, 42, 0.125
        )
        self.settings: dict[str, Any] = {
            "experiment_id": "test",
            "dataset": {
                "path": str(self.data), "sha256": sha256(self.data), "target": "y",
                "target_unit": "kcal/mol", "row_id": "id", "smiles_column": "rxn",
                "group_columns": ["a", "b"],
            },
            "split": {"folds": 5, "seed": 42, "validation_fraction": 0.125},
        }

    def test_unknown_nested_fields_are_rejected_before_preparation(self) -> None:
        for section, unknown in (("dataset", "target_column"), ("split", "paths"), ("split", "seeds")):
            with self.subTest(section=section, field=unknown):
                value = {**self.settings, section: {**self.settings[section], unknown: "typo"}}
                config, run = self.root / "request.toml", self.root / "rejected"
                dump_toml(config, value)
                with self.assertRaisesRegex(ValueError, f"unknown benchmark {section} fields: {unknown}"):
                    prepare(config, run)
                self.assertFalse(run.exists())

    def test_split_binding_must_be_complete_and_nonempty(self) -> None:
        for binding in (
            {"sha256": "0" * 64}, {"path": "existing.csv"},
            {"path": "", "sha256": "0" * 64}, {"path": " ", "sha256": "0" * 64},
            {"path": False, "sha256": "0" * 64}, {"path": "existing.csv", "sha256": ""},
            {"path": "existing.csv", "sha256": False},
        ):
            with self.subTest(binding=binding):
                config, run = self.root / "request.toml", self.root / "rejected"
                dump_toml(config, {**self.settings, "split": {**self.settings["split"], **binding}})
                with self.assertRaisesRegex(ValueError, "split\\."):
                    prepare(config, run)
                self.assertFalse(run.exists())

    def test_existing_splits_are_checked_and_preserved_without_regeneration(self) -> None:
        source = self.root / "existing.csv"
        write_rows(source, list(reversed(make_splits(self.rows, self.request))))
        value = {**self.settings, "split": {
            **self.settings["split"], "path": source.name, "sha256": sha256(source),
        }}
        config, run = self.root / "request.toml", self.root / "prepared"
        dump_toml(config, value)
        prepare(config, run)
        self.assertEqual((run / "splits.csv").read_bytes(), source.read_bytes())
        source.write_bytes(source.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "split SHA-256 mismatch"):
            prepare(config, self.root / "rejected")
        source.unlink()
        with self.assertRaises(FileNotFoundError):
            prepare(config, self.root / "rejected")
        self.assertFalse((self.root / "rejected").exists())

    def test_groups_are_disjoint_and_oof_is_exact(self) -> None:
        splits = make_splits(self.rows, self.request)
        lookup = {r["id"]: r["a"] for r in self.rows}
        for fold in range(5):
            assignments = [r for r in splits if r["fold"] == fold]
            self.assertEqual(Counter(r["row_id"] for r in assignments), Counter(lookup.keys()))
            groups = {
                role: {lookup[r["row_id"]] for r in assignments if r["split"] == role}
                for role in ("train", "val", "test")
            }
            self.assertTrue(all(groups.values()))
            self.assertFalse(groups["train"] & groups["val"])
            self.assertFalse(groups["train"] & groups["test"])
            self.assertFalse(groups["val"] & groups["test"])
        self.assertEqual(
            Counter(r["row_id"] for r in splits if r["split"] == "test"), Counter(lookup.keys())
        )

    def test_splits_ignore_labels_and_input_row_order(self) -> None:
        expected = make_splits(self.rows, self.request)
        changed = [{**r, "y": "9999"} for r in reversed(self.rows)]
        self.assertEqual(expected, make_splits(changed, self.request))
        self.assertNotEqual(expected, make_splits(self.rows, replace(self.request, seed=8)))

    def test_changed_data_and_duplicate_ids_fail_closed(self) -> None:
        self.assertEqual(validated_rows(self.request), self.rows)
        self.data.write_text(self.data.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            validated_rows(self.request)
        write_rows(self.data, self.rows + [self.rows[0]])
        with self.assertRaisesRegex(ValueError, "duplicate row ID"):
            validated_rows(replace(self.request, expected_sha256=sha256(self.data)))

    def test_nonfinite_targets_are_rejected(self) -> None:
        write_rows(self.data, [{**self.rows[0], "y": "nan"}])
        with self.assertRaisesRegex(ValueError, "non-finite"):
            validated_rows(replace(self.request, expected_sha256=sha256(self.data)))

    def test_metrics_are_pooled_errors_not_mean_fold_rmse(self) -> None:
        metrics = regression_metrics([0, 0, 0], [0, 0, 3])
        self.assertEqual(metrics["mae"], 1)
        self.assertAlmostEqual(metrics["rmse"], 3**0.5)
        self.assertNotIn("r2", metrics)  # undefined for a constant target
        with self.assertRaises(ValueError):
            regression_metrics([1], [float("inf")])

    def test_registry_requires_explicit_model_ids(self) -> None:
        self.assertEqual(
            validate_models(["chemprop_cgr", "chemprop_multicomponent", "rxnresid"]),
            ["chemprop_cgr", "chemprop_multicomponent", "rxnresid"],
        )
        with self.assertRaises(ValueError):
            validate_models(["unimol"])
        with self.assertRaises(ValueError):
            validate_models(["chemprop", "chemprop"])

    def test_reactiont5_is_a_separate_extension(self) -> None:
        self.assertEqual(validate_models(["t5chem", "reactiont5"]), ["t5chem", "reactiont5"])
        asset = self.root / "weights.bin"
        self.assertEqual(
            verify_asset_files(self.root, [{"path": "weights.bin", "sha256": "0" * 64}]), [str(asset)]
        )
        asset.write_bytes(b"checkpoint fixture")
        manifest = [{"path": "weights.bin", "sha256": sha256(asset)}]
        self.assertEqual(verify_asset_files(self.root, manifest), [])
        asset.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            verify_asset_files(self.root, manifest)

    def test_collection_rejects_misaligned_and_duplicate_predictions(self) -> None:
        run = self.root / "collection"
        run.mkdir()
        write_rows(run / "dataset.csv", self.rows)
        splits = make_splits(self.rows, self.request)
        write_rows(run / "splits.csv", splits)
        from tricycle_sar.training import protocol
        method, manifest_hash = protocol("t5chem")
        dump_toml(
            run / "run.toml",
            {
                "models": ["t5chem"],
                "training_protocol_manifest": method["manifest"],
                "training_protocol_manifest_sha256": manifest_hash,
                "methods": {"t5chem": method["method_id"]},
                "input_hashes": [{"path": name, "sha256": sha256(run / name)} for name in ("dataset.csv", "splits.csv")],
                "row_id": "id",
                "target": "y",
                "group_columns": ["a", "b"],
                "dataset_sha256": sha256(run / "dataset.csv"),
            },
        )
        for fold in range(5):
            ids = {r["row_id"] for r in splits if r["fold"] == fold and r["split"] == "test"}
            predictions = [
                {
                    "row_index": i,
                    "reaction_id": r["id"],
                    "target_name": "y",
                    "y_true": r["y"],
                    "y_pred": float(r["y"]) + 1,
                }
                for i, r in enumerate(self.rows)
                if r["id"] in ids
            ]
            write_rows(prediction_file(run, "t5chem", fold), predictions)
        self.assertEqual(collect(run)["t5chem"]["pooled"]["mae"], 1)
        file = prediction_file(run, "t5chem", 0)
        original = read_rows(file)
        write_rows(file, original + [original[0]])
        with self.assertRaisesRegex(ValueError, "OOF coverage"):
            collect(run)
        write_rows(file, [{**r, "y_true": float(r["y_true"]) + 2e-6} for r in original])
        self.assertEqual(collect(run)["t5chem"]["pooled"]["mae"], 1)
        write_rows(file, [{**r, "y_true": "999"} for r in original])
        with self.assertRaisesRegex(ValueError, "identity/target"):
            collect(run)
        (run / "dataset.csv").write_text("changed")
        with self.assertRaisesRegex(ValueError, "frozen artifact"):
            check(run)

    def test_prepare_preserves_source_and_refuses_reuse(self) -> None:
        cfg = self.root / "request.toml"
        cfg.write_text(f'''experiment_id = "test"
[dataset]
path = "data.csv"
sha256 = "{sha256(self.data)}"
target = "y"
target_unit = "kcal/mol"
row_id = "id"
smiles_column = "rxn"
group_columns = ["a", "b"]
[split]
folds = 5
seed = 42
validation_fraction = 0.125
''')
        run = prepare(cfg, self.root / "run")
        self.assertEqual(sha256(run / "dataset.csv"), sha256(self.data))
        self.assertEqual(len(read_rows(run / "splits.csv")), len(self.rows) * 5)
        with self.assertRaisesRegex(ValueError, "empty"):
            prepare(cfg, run)


if __name__ == "__main__":
    unittest.main()
