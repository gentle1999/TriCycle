"""Small scientific result packages, independent of local experiment archives."""

import hashlib
from collections.abc import Callable
from itertools import count
from pathlib import Path

import pytest
from tricycle_core.io import file_sha256
from tricycle_core.training_protocol import resolve_training_registry

from tricycle_sar.backends import CATALOG
from tricycle_sar.benchmark import BenchmarkRequest, make_splits, write_rows
from tricycle_sar.records import publication_files
from tricycle_sar.results import publish, read, verify


@pytest.fixture
def result_package_factory(tmp_path: Path) -> Callable[..., Path]:
    sequence = count()

    def create(model: str = "fingerprint_rf", *, recorded: bool = False) -> Path:
        root = tmp_path / f"fixture-{next(sequence)}"
        root.mkdir()
        rows = [
            {
                "path_id": f"p{index}",
                "rxn_smiles": "C>>C",
                "G_T_activate": str(index + 1),
                "reaction_free_energy": "0",
                "ene_id": str(index),
                "diene_id": "1",
                "prod_id": "route-a",
            }
            for index in range(17)
        ]
        dataset, splits = root / "dataset.csv", root / "splits.csv"
        write_rows(dataset, rows)
        request = BenchmarkRequest(
            dataset,
            file_sha256(dataset),
            "G_T_activate",
            "path_id",
            ("ene_id", "diene_id"),
            "rxn_smiles",
            5,
            42,
            0.125,
        )
        assignments = make_splits(rows, request)
        write_rows(splits, assignments)
        manifest, registry = resolve_training_registry(CATALOG)
        method = registry["models"][model]["default_method"]
        bindings = {(model, fold): method for fold in range(5)}
        predictions = {}
        for fold in range(5):
            ids = {
                row["row_id"]
                for row in assignments
                if row["fold"] == fold and row["split"] == "test"
            }
            path = root / f"worker-{fold}/predictions.csv"
            write_rows(
                path,
                [
                    {
                        "reaction_id": row["path_id"],
                        "row_index": index,
                        "target_name": "G_T_activate",
                        "y_true": float(row["G_T_activate"]) + 1e-7,
                        "y_pred": float(row["G_T_activate"]) + 1,
                    }
                    for index, row in enumerate(rows)
                    if row["path_id"] in ids
                ],
            )
            path.with_name("install.log").write_text("runtime only")
            predictions[model, fold] = path
        output = publish(
            dataset,
            splits,
            predictions,
            bindings,
            CATALOG,
            root / "published",
            expected_protocol_sha256=file_sha256(manifest),
        )
        if recorded:
            # A recorded method need not have executable code in the current catalog.
            table = output / "METHOD_BINDINGS.csv"
            entries = read(table)
            for entry in entries:
                fingerprint = hashlib.sha256(f"synthetic-recorded/{model}".encode()).hexdigest()[
                    :16
                ]
                entry["method_id"] = f"{model}/train/{fingerprint}"
            write_rows(table, entries)
            (output / "SHA256SUMS").write_text(
                "".join(
                    f"{file_sha256(path)}  {path.relative_to(output).as_posix()}\n"
                    for path in publication_files(output)
                    if path.name != "SHA256SUMS"
                )
            )
            verify(output)
        return output

    return create
