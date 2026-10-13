from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tricycle_core.io import file_sha256

from tricycle_sar.benchmark import BenchmarkRequest, make_splits, write_rows
from tricycle_sar.classical_runner import MODELS, fingerprint, read_csv, run_fold
from tricycle_sar.modelling_benchmark import validate_models


@unittest.skipUnless(
    importlib.util.find_spec("sklearn") and importlib.util.find_spec("rdkit"),
    "install tricycle-sar[classical] for CPU baseline tests",
)
class ClassicalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows = [
            {
                "id": str(i),
                "group": str(i // 2),
                "y": str(i / 10),
                "smiles": ("C=C.C=CC=C>>C1CCCCC1" if i % 2 else "CC=C.C=CC=C>>CC1CCCCC1"),
            }
            for i in range(32)
        ]
        self.dataset = self.root / "data.csv"
        self.splits = self.root / "splits.csv"
        write_rows(self.dataset, self.rows)
        request = BenchmarkRequest(
            self.dataset, file_sha256(self.dataset), "y", "id", ("group",), "smiles", 5, 42, 0.125
        )
        self.assignments = make_splits(self.rows, request)
        write_rows(self.splits, self.assignments)
        self.config: dict[str, Any] = {
            "dataset": str(self.dataset),
            "splits": str(self.splits),
            "dataset_sha256": file_sha256(self.dataset),
            "splits_sha256": file_sha256(self.splits),
            "row_id": "id",
            "target": "y",
            "smiles_column": "smiles",
            "group_columns": ["group"],
            "output": str(self.root / "models"),
            "smoke_output": str(self.root / "smoke"),
            "versions": {},
            "seed": 42,
            "n_jobs": 1,
            "fingerprint": {"radius": 2, "size": 64, "include_chirality": True},
            "models": {
                "fingerprint_rf": {"n_estimators": 8},
                "fingerprint_extra_trees": {"n_estimators": 8},
                "fingerprint_ridge": {"alpha": 10.0},
            },
        }

    def test_mapping_and_component_order_do_not_change_features(self) -> None:
        import numpy as np

        spec = self.config["fingerprint"]
        first = fingerprint("[CH2:1]=[CH2:2].C=CC=C>>C1CCCCC1", spec)
        second = fingerprint("C=CC=C.[CH2:93]=[CH2:5]>>C1CCCCC1", spec)
        np.testing.assert_array_equal(first, second)
        self.assertTrue((first[128:192] < 0).any())  # subtraction is signed
        self.assertFalse(np.array_equal(first, fingerprint("C=C.C=CC=C>O>C1CCCCC1", spec)))
        with self.assertRaises(ValueError):
            fingerprint("not-a-reaction", spec)

    def test_chirality_is_retained(self) -> None:
        import numpy as np

        spec = {**self.config["fingerprint"], "size": 2048}
        a = fingerprint("CC(O)F>>C[C@H](O)F", spec)
        b = fingerprint("CC(O)F>>C[C@@H](O)F", spec)
        self.assertFalse(np.array_equal(a, b))

    def test_all_models_fit_predict_and_smoke_does_not_make_benchmark_outputs(self) -> None:
        import math
        import tomllib

        self.assertEqual(validate_models(list(MODELS)), list(MODELS))
        for model in MODELS:
            out = run_fold(self.config, model, 0, smoke=True)
            self.assertTrue(
                all(math.isfinite(float(p["y_pred"])) for p in read_csv(out / "predictions.csv"))
            )
            report = tomllib.loads((out / "runtime.toml").read_text())
            self.assertEqual(report["fit_splits"], ["train"])
            self.assertFalse(report["benchmark_result"])
            with self.assertRaises(FileExistsError):
                run_fold(self.config, model, 0, smoke=True)
        self.assertFalse(Path(self.config["output"]).exists())

    def test_held_out_labels_do_not_affect_fit_or_scaler(self) -> None:
        first = run_fold(self.config, "fingerprint_ridge", 0, smoke=True)
        prediction = [r["y_pred"] for r in read_csv(first / "predictions.csv")]
        held_out = {
            r["row_id"] for r in self.assignments if r["fold"] == 0 and r["split"] != "train"
        }
        write_rows(
            self.dataset,
            [{**r, "y": "999999" if r["id"] in held_out else r["y"]} for r in self.rows],
        )
        changed = {
            **self.config,
            "dataset_sha256": file_sha256(self.dataset),
            "smoke_output": str(self.root / "changed"),
        }
        second = run_fold(changed, "fingerprint_ridge", 0, smoke=True)
        self.assertEqual(prediction, [r["y_pred"] for r in read_csv(second / "predictions.csv")])

    def test_input_mutation_and_group_leakage_are_rejected(self) -> None:
        self.dataset.write_text(self.dataset.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            run_fold(self.config, "fingerprint_rf", 0)
        self.config["dataset_sha256"] = file_sha256(self.dataset)
        part = [r for r in self.assignments if r["fold"] == 0]
        train_id = next(r["row_id"] for r in part if r["split"] == "train")
        corrupted = [
            {**r, "split": "test" if r["fold"] == 0 and r["row_id"] == train_id else r["split"]}
            for r in self.assignments
        ]
        write_rows(self.splits, corrupted)
        self.config["splits_sha256"] = file_sha256(self.splits)
        with self.assertRaisesRegex(ValueError, "leakage"):
            run_fold(self.config, "fingerprint_rf", 0)
