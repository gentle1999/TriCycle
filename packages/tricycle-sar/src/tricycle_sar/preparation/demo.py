"""Prepare the current sampling experiment demo using the shared RxnResid backend."""

from __future__ import annotations

import csv
import math
from pathlib import Path

from tricycle_core.io import copy_snapshot_file, write_json
from tricycle_core.toml import dump_toml

from tricycle_sar.assets import load_model_asset
from tricycle_sar.sampling_runtime import stage, worker_launcher

SAMPLING_COLUMNS = ("path_id", "rxn_smiles", "ene_id", "diene_id", "prod_id", "G_T_activate")


def validate_sampling_dataset(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if not set(SAMPLING_COLUMNS).issubset(reader.fieldnames or ()):
            raise ValueError(
                "This sampling demo requires experiment columns: " + ", ".join(SAMPLING_COLUMNS)
            )
        rows = list(reader)
    if not rows or any(not row.get(key, "").strip() for row in rows for key in SAMPLING_COLUMNS):
        raise ValueError("Sampling demo rows must have complete identities and targets")
    if len({row["path_id"] for row in rows}) != len(rows):
        raise ValueError("Sampling demo path IDs must be unique")
    if any(not math.isfinite(float(row["G_T_activate"])) for row in rows):
        raise ValueError("Sampling demo targets must be finite")
    return rows


def prepare_demo(*, export_path: Path, source: Path, root: Path) -> dict[str, object]:
    rows = validate_sampling_dataset(export_path)
    if root.exists():
        raise FileExistsError(f"Refusing to replace demo inputs: {root}")
    asset = load_model_asset()
    record = stage(root, {"source_directory": str(source), "revision": asset.revision})
    data = root / "training.csv"
    copy_snapshot_file(export_path, data)
    request = {
        "dataset": "training.csv",
        "sar_backend": record,
        "training": {
            "epochs": 300,
            "batch_size_paths": 64,
            "device": "auto",
            "mixed_precision": "no",
            "seed": 42,
        },
    }
    dump_toml(root / "request.toml", request)
    (root / "runner.py").write_text("from tricycle_sar.preparation.demo import main\nmain()\n")
    worker = root / "worker.sh"
    worker.write_text(worker_launcher())
    worker.chmod(0o755)
    write_json(
        root / "provenance.json",
        {
            "schema": "tricycle.sar-demo/v2",
            "backend": record,
            "source_export": str(export_path.resolve()),
            "native_identity_policy": "preserve supplied component and path IDs",
        },
    )
    return {
        "input_directory": str(root),
        "dataset": str(data),
        "request": str(root / "request.toml"),
        "row_count": len(rows),
        "group_count": len({(r["ene_id"], r["diene_id"]) for r in rows}),
        "command": ["bash", "worker.sh", "request.toml"],
        "submitted": False,
    }


def main() -> None:
    import argparse

    from tricycle_core.toml import load_toml

    from tricycle_sar.rxnresid import fit_all_revealed
    from tricycle_sar.sampling_runtime import verify

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    root = args.request.resolve().parent
    request = load_toml(args.request)
    verify(root, request["sar_backend"])
    validate_sampling_dataset(root / request["dataset"])
    fit_all_revealed(
        root / request["dataset"], root / "outputs", request["training"], source=root / "rxnresid"
    )


if __name__ == "__main__":
    main()
