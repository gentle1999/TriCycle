"""Frozen, group-disjoint cross-validation for reaction regression benchmarks.

Preparation is local: no model imports, credentials, networking, or submission.
"""

from __future__ import annotations

import csv
import math
import random
import re
import shutil
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tricycle_core import timestamped_run_directory
from tricycle_core.io import file_sha256 as sha256
from tricycle_core.metrics import regression_metrics as regression_metrics
from tricycle_core.toml import dump_toml, load_toml
from tricycle_core.validation import validate_fields

SCHEMA = "tricycle.sar-benchmark/v1"


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError("cannot write an empty table")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@dataclass(frozen=True)
class BenchmarkRequest:
    dataset: Path
    expected_sha256: str
    target: str
    row_id: str
    group_columns: tuple[str, ...]
    smiles_column: str
    folds: int
    seed: int
    validation_fraction: float

    @classmethod
    def from_mapping(cls, value: dict[str, Any], base: Path) -> BenchmarkRequest:
        dataset, split = value["dataset"], value["split"]
        for name, section, allowed in (
            (
                "dataset", dataset,
                {"path", "sha256", "target", "target_unit", "row_id", "group_columns",
                 "smiles_column", "reaction_energy_column", "expected_rows", "expected_groups"},
            ),
            ("split", split, {"path", "sha256", "folds", "seed", "validation_fraction"}),
        ):
            if not isinstance(section, Mapping):
                raise ValueError(f"benchmark {name} must be a table")
            validate_fields(section, allowed, context=f"benchmark {name}")
        if ("path" in split) != ("sha256" in split):
            raise ValueError("split.path and split.sha256 must be provided together")
        if "path" in split:
            if not isinstance(split["path"], str) or not split["path"].strip():
                raise ValueError("split.path must be a nonempty string")
            if not isinstance(split["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", split["sha256"]):
                raise ValueError("split.sha256 must be a SHA-256 identity")
        path = Path(dataset["path"])
        request = cls(
            dataset=path.resolve() if path.is_absolute() else (base / path).resolve(),
            expected_sha256=str(dataset["sha256"]),
            target=str(dataset.get("target", "G_T_activate")),
            row_id=str(dataset.get("row_id", "path_id")),
            group_columns=tuple(dataset.get("group_columns", ["ene_id", "diene_id"])),
            smiles_column=str(dataset.get("smiles_column", "rxn_smiles")),
            folds=int(split.get("folds", 5)),
            seed=int(split.get("seed", 42)),
            validation_fraction=float(split.get("validation_fraction", 0.2)),
        )
        if request.folds != 5:
            raise ValueError("this benchmark protocol requires exactly five outer folds")
        if not 0 < request.validation_fraction < 0.5:
            raise ValueError("validation_fraction must lie between zero and 0.5")
        if not request.group_columns or not re.fullmatch(r"[a-f0-9]{64}", request.expected_sha256):
            raise ValueError("explicit group columns and a SHA-256 dataset identity are required")
        if dataset.get("target_unit") != "kcal/mol":
            raise ValueError("target_unit must be kcal/mol")
        return request


def validated_rows(request: BenchmarkRequest) -> list[dict[str, str]]:
    if sha256(request.dataset) != request.expected_sha256:
        raise ValueError("dataset SHA-256 mismatch; freeze a new configuration for changed data")
    rows = read_rows(request.dataset)
    if not rows:
        raise ValueError("dataset is empty")
    required = {request.target, request.row_id, request.smiles_column, *request.group_columns}
    ids: set[str] = set()
    for row in rows:
        if any(not row.get(key, "").strip() for key in required):
            raise ValueError("dataset contains missing required values")
        if row[request.row_id] in ids:
            raise ValueError("duplicate row ID")
        ids.add(row[request.row_id])
        if not math.isfinite(float(row[request.target])):
            raise ValueError("non-finite target")
        if row[request.smiles_column].count(">") != 2:
            raise ValueError("reaction SMILES must contain reactants>agents>products")
    return rows


def make_splits(rows: list[dict[str, str]], request: BenchmarkRequest) -> list[dict[str, Any]]:
    """Shuffle whole groups, then balance folds by row count without target access."""
    groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for row in rows:
        key = tuple(row[column] for column in request.group_columns)
        groups[key].append(row[request.row_id])
    if len(groups) < request.folds + 2:
        raise ValueError("not enough substrate groups for test, train, and validation")
    keys = sorted(groups)
    random.Random(request.seed).shuffle(keys)
    keys.sort(key=lambda key: len(groups[key]), reverse=True)  # stable seeded ties
    bins: list[list[tuple[str, ...]]] = [[] for _ in range(request.folds)]
    sizes = [0] * request.folds
    for key in keys:
        index = min(range(request.folds), key=lambda i: (sizes[i], i))
        bins[index].append(key)
        sizes[index] += len(groups[key])
    result = []
    for fold in range(request.folds):
        test = set(bins[fold])
        remaining = sorted(set(groups) - test)
        random.Random(request.seed + 1000 + fold).shuffle(remaining)
        n_valid = max(1, round(len(remaining) * request.validation_fraction))
        valid = set(remaining[:n_valid])
        train = set(remaining[n_valid:])
        if not train:
            raise ValueError("empty training split")
        for row in sorted(rows, key=lambda item: item[request.row_id]):
            key = tuple(row[column] for column in request.group_columns)
            role = "test" if key in test else "val" if key in valid else "train"
            result.append({"fold": fold, "row_id": row[request.row_id], "split": role})
    return result


def prepare(config: Path, run_dir: Path | None = None, *, settings: dict | None = None) -> Path:
    """Freeze inputs and group splits; existing runs are never overwritten."""
    from tricycle_core.training_protocol import expand_training_config

    value = settings if settings is not None else expand_training_config(config, load_toml(config))
    request = BenchmarkRequest.from_mapping(value, config.resolve().parent)
    rows = validated_rows(request)
    split_input = value.get("split", {}).get("path")
    if split_input:
        split_path = Path(split_input)
        if not split_path.is_absolute():
            split_path = config.resolve().parent / split_path
        if sha256(split_path) != value["split"]["sha256"]:
            raise ValueError("frozen split SHA-256 mismatch")
        splits = read_rows(split_path)
    else:
        split_path = None
        splits = make_splits(rows, request)
    from .training_inputs import validate_splits

    validate_splits(
        rows,
        splits,
        {"row_id": request.row_id, "group_columns": request.group_columns},
        request.folds,
    )
    name = str(value.get("experiment_id", "ground-truth-baselines-5fold"))
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise ValueError("unsafe experiment ID")
    destination = run_dir or timestamped_run_directory(Path("runs"), name)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("run directory must be empty; use a new run for changed data")
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(request.dataset, destination / "dataset.csv")
    if split_path is None:
        write_rows(destination / "splits.csv", splits)
    else:
        shutil.copy2(split_path, destination / "splits.csv")
    binding = value.get("training_protocol", {})
    dump_toml(
        destination / "run.toml",
        {
            "schema": SCHEMA,
            "experiment_id": name,
            "status": "prepared",
            "dataset_sha256": request.expected_sha256,
            "splits_sha256": sha256(destination / "splits.csv"),
            "rows": len(rows),
            "groups": len({tuple(r[c] for c in request.group_columns) for r in rows}),
            "folds": request.folds,
            "seed": request.seed,
            "group_columns": list(request.group_columns),
            "row_id": request.row_id,
            "target": request.target,
            "smiles_column": request.smiles_column,
            "target_unit": "kcal/mol",
            "split_protocol": "seeded-greedy-group-5fold/v1",
            "validation_fraction": request.validation_fraction,
            **(
                {
                    "training_protocol_manifest": binding["manifest"],
                    "training_protocol_manifest_sha256": binding["sha256"],
                }
                if binding
                else {}
            ),
        },
    )
    from .delivery import freeze

    freeze(destination, (destination / "dataset.csv", destination / "splits.csv"))
    return destination.resolve()
