"""Verify recorded scientific results and prepare new runs using current backends."""

from __future__ import annotations

import csv
import json
import math
import statistics
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tricycle_core.io import file_sha256 as sha256
from tricycle_core.metrics import regression_metrics

from .predictions import identity


def read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def local(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Nonlocal archive reference: {relative}")
    return path


def close(actual: float, expected: float, tolerance: float, context: str) -> None:
    if (
        not math.isfinite(actual)
        or not math.isfinite(expected)
        or abs(actual - expected) > tolerance
    ):
        raise ValueError(f"{context}: {actual} != {expected}")


def prepare_training(
    root: Path,
    model: str,
    fold: int,
    output: Path,
    *,
    assets: Path | None = None,
    retain_checkpoint: bool = False,
) -> dict:
    """Prepare a new run with published data and the current model backend."""
    from .records import read_method_bindings
    from .training_inputs import prepare_inputs

    root, output = root.resolve(), output.resolve()
    verify(root)
    if output.is_relative_to(root):
        raise ValueError("Prepare inputs outside the published result package")
    bindings = read_method_bindings(root / "METHOD_BINDINGS.csv")
    if (model, fold) not in bindings:
        raise ValueError(f"Unknown published model/fold: {model}/{fold}")
    from .backends.registry import CATALOG

    result = prepare_inputs(
        root / "data/dataset.csv",
        root / "data/splits.csv",
        CATALOG,
        model,
        fold,
        output,
        assets=assets,
        retain_checkpoint=retain_checkpoint,
    )
    return result


def index_assets(root: Path, files: list[Path], output: Path) -> dict:
    """Reuse hash-addressed, locally supplied assets required by published methods."""
    import shutil

    from .records import read_method_bindings

    root, output = root.resolve(), output.resolve()
    verify(root)
    if output.is_relative_to(root):
        raise ValueError("Index assets outside the published result package")
    if not files:
        raise ValueError("No fixed assets provided")
    from tricycle_core.training_protocol import resolve_training_registry

    from .backends import CATALOG

    _, registry = resolve_training_registry(CATALOG)
    models = {model for model, _ in read_method_bindings(root / "METHOD_BINDINGS.csv")}
    expected = {
        entry["sha256"]: entry
        for model in models
        for entry in registry["methods"][registry["models"][model]["default_method"]]["files"]
        if entry["external"] or entry["path"].endswith(".whl")
    }
    validated = {}
    for path in files:
        digest = sha256(path)
        if digest not in expected or path.stat().st_size != expected[digest]["bytes"]:
            raise ValueError(f"Unrecognized fixed asset: {path}")
        target = output / digest
        if target.exists() and sha256(target) != digest:
            raise ValueError(f"Existing indexed asset is corrupted: {target}")
        validated[digest] = path
    output.mkdir(parents=True, exist_ok=True)
    for digest, path in validated.items():
        target = output / digest
        if not target.exists():
            shutil.copyfile(path, target)
        if sha256(target) != digest:
            raise ValueError(f"Asset copy verification failed: {target}")
    return {"indexed": list(validated), "network": False}


def metrics(rows: list[dict[str, str]]) -> dict[str, float]:
    values = regression_metrics(
        [float(r["y_true"]) for r in rows], [float(r["y_pred"]) for r in rows]
    )
    return {key: float(values[key]) for key in ("mae", "rmse", "r2") if key in values}


def verify(root: Path, *, integrity: bool = True) -> dict:
    root = root.resolve()
    if integrity:
        checksums = root / "SHA256SUMS"
        if not checksums.exists():
            raise ValueError("Missing SHA256SUMS")
        indexed = set()
        for line in checksums.read_text().splitlines():
            expected_hash, name = line.split("  ", 1)
            if name in indexed or len(expected_hash) != 64:
                raise ValueError(f"Invalid or duplicate checksum entry: {name}")
            indexed.add(name)
            if sha256(local(root, name)) != expected_hash:
                raise ValueError(f"Archive hash mismatch: {name}")
        from .records import publication_files

        actual = {
            path.relative_to(root).as_posix()
            for path in publication_files(root)
            if path != checksums
        }
        if actual != indexed:
            raise ValueError("Archive files differ from the checksum inventory")
    manifest = json.loads((root / "manifest.json").read_text())
    for name, hash_key in [
        ("data/dataset.csv", "dataset_sha256"),
        ("data/splits.csv", "splits_sha256"),
    ]:
        if sha256(root / name) != manifest[hash_key]:
            raise ValueError(f"Frozen input mismatch: {name}")
    # Method IDs and backend hashes record what produced the observations. They
    # are not executable source references and need no retained implementation.
    if "training_protocol_manifest_sha256" in manifest:
        raise ValueError("SAR result manifest requires backend.sha256; old hash fields are unsupported")
    backend = manifest.get("backend")
    if not isinstance(backend, dict):
        raise ValueError("SAR result manifest requires a backend object")
    recorded_hash = backend.get("sha256")
    if not isinstance(recorded_hash, str) or len(recorded_hash) != 64 or any(
        char not in "0123456789abcdef" for char in recorded_hash
    ):
        raise ValueError("Invalid recorded backend hash")
    data = read(root / "data/dataset.csv")
    by_id = {row["path_id"]: row for row in data}
    index = {row["path_id"]: i for i, row in enumerate(data)}
    if len(data) != manifest["rows_per_model"] or len(by_id) != len(data):
        raise ValueError("Dataset coverage mismatch")
    folds = range(manifest["folds"])
    assignments: dict[int, dict[str, str]] = {fold: {} for fold in folds}
    for row in read(root / "data/splits.csv"):
        fold, path_id, role = int(row["fold"]), row["row_id"], row["split"]
        if (
            fold not in assignments
            or path_id not in by_id
            or path_id in assignments[fold]
            or role not in {"train", "val", "test"}
        ):
            raise ValueError("Invalid split assignment")
        assignments[fold][path_id] = role
    test_count: defaultdict[str, int] = defaultdict(int)
    for fold, split in assignments.items():
        if set(split) != set(by_id):
            raise ValueError(f"Incomplete split: fold {fold}")
        roles = defaultdict(set)
        for path_id, role in split.items():
            row = by_id[path_id]
            roles[role].add(identity(row, ("ene_id", "diene_id")))
            test_count[path_id] += role == "test"
        for a, b in [("train", "val"), ("train", "test"), ("val", "test")]:
            if not roles[a] or not roles[b] or roles[a] & roles[b]:
                raise ValueError(f"Group leakage or empty role: fold {fold}, {a}/{b}")
    if any(test_count[path_id] != 1 for path_id in by_id):
        raise ValueError("Each path must occur in one test fold")
    from .records import read_method_bindings

    bindings = read_method_bindings(root / "METHOD_BINDINGS.csv")
    expected = {(model, fold) for model in manifest["models"] for fold in folds}
    if bindings.keys() != expected:
        raise ValueError("Incomplete method bindings")
    for (model, _), method_id in bindings.items():
        prefix = f"{model}/train/"
        fingerprint = method_id.removeprefix(prefix)
        if not method_id.startswith(prefix) or len(fingerprint) != 16 or any(
            char not in "0123456789abcdef" for char in fingerprint
        ):
            raise ValueError("Invalid recorded method binding")
    oof = read(root / "results/oof-predictions.csv")
    expected_predictions = {(model, path_id) for model in manifest["models"] for path_id in by_id}
    seen, grouped = set(), defaultdict(list)
    for row in oof:
        key = row["model"], row["path_id"]
        fold = int(row["fold"])
        if (
            key in seen
            or key not in expected_predictions
            or fold not in assignments
            or assignments[fold][row["path_id"]] != "test"
        ):
            raise ValueError("OOF duplicates, unknown identities or non-test predictions")
        seen.add(key)
        if int(row["row_index"]) != index[row["path_id"]]:
            raise ValueError("OOF row index mismatch")
        close(
            float(row["y_true"]),
            float(by_id[row["path_id"]][manifest["target"]]),
            1e-12,
            "canonical GT",
        )
        close(
            float(row["reported_y_true"]),
            float(by_id[row["path_id"]][manifest["target"]]),
            manifest["native_target_tolerance"],
            "native GT",
        )
        prediction = float(row["y_pred"])
        close(prediction, prediction, 0, "finite prediction")
        close(
            float(row["absolute_error"]),
            abs(float(row["y_pred"]) - float(row["y_true"])),
            1e-12,
            "absolute error",
        )
        grouped[row["model"]].append(row)
    if seen != expected_predictions:
        raise ValueError("Incomplete OOF table")
    published_folds = {
        (r["model"], int(r["fold"])): r for r in read(root / "results/fold-metrics.csv")
    }
    published_models = {r["model"]: r for r in read(root / "results/model-summary.csv")}
    if set(published_folds) != expected or set(published_models) != set(manifest["models"]):
        raise ValueError("Incomplete published metric table")
    computed = {}
    for model in manifest["models"]:
        rows = grouped[model]
        if len(rows) != len(data):
            raise ValueError("Incorrect per-model OOF coverage")
        fold_maes = []
        for fold in folds:
            selected = [r for r in rows if int(r["fold"]) == fold]
            values = metrics(selected)
            published = published_folds[model, fold]
            if len(selected) != int(published["n_test"]):
                raise ValueError("Fold row count mismatch")
            for name, value in values.items():
                close(
                    value,
                    float(published[name]),
                    manifest["metric_tolerance"],
                    f"{model}/{fold}/{name}",
                )
            fold_maes.append(values["mae"])
        values = {
            **{f"oof_{name}": value for name, value in metrics(rows).items()},
            "fold_mae_mean": statistics.mean(fold_maes),
            "fold_mae_sd": statistics.stdev(fold_maes),
        }
        for name, value in values.items():
            close(
                value,
                float(published_models[model][name]),
                manifest["metric_tolerance"],
                f"{model}/{name}",
            )
        computed[model] = values
    return {
        "status": "verified",
        "models": len(computed),
        "folds": len(bindings),
        "rows_per_model": len(data),
        "oof_rows": len(oof),
        "training_methods": len(set(bindings.values())),
        "protocol_manifest_sha256": recorded_hash,
        "verification_scope": "recorded-scientific-results",
        "metrics": computed,
    }


@contextmanager
def publication_directory(destination: Path) -> Iterator[Path]:
    """Validate a complete publication before exposing its final directory."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".sar-publication-", dir=destination.parent
    ) as temporary:
        staging = Path(temporary)
        yield staging
        if destination.exists():
            raise FileExistsError(f"Refusing to replace results: {destination}")
        staging.rename(destination)


def publish(
    dataset: Path,
    splits: Path,
    predictions: dict | Path,
    bindings: dict,
    reference: Path,
    output: Path,
    *,
    expected_protocol_sha256: str,
) -> Path:
    """Export one OOF prediction table; worker outputs remain in the source run."""
    import os
    import shutil

    from tricycle_core.training_protocol import file_sha256, resolve_training_registry

    from .benchmark import write_rows
    from .records import publication_files, write_method_bindings
    from .training_inputs import validate_splits

    protocol_manifest, registry = resolve_training_registry(reference, expected_protocol_sha256)
    if file_sha256(protocol_manifest) != expected_protocol_sha256:
        raise ValueError("Experiment training protocol differs")
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to replace results: {output}")
    data, assignments = read(dataset), read(splits)
    settings = registry["benchmark_defaults"]["dataset"]
    validate_splits(data, assignments, settings)
    models = sorted({model for model, _ in bindings})
    expected = {(model, fold) for model in models for fold in range(5)}
    if isinstance(predictions, Path):
        grouped_predictions = defaultdict(list)
        for row in read(predictions):
            grouped_predictions[row["model"], int(row["fold"])].append(row)
        predictions = grouped_predictions
    if not models or predictions.keys() != expected or bindings.keys() != expected:
        raise ValueError("Publishing requires complete five-fold predictions and method bindings")
    indices = {row[settings["row_id"]]: index for index, row in enumerate(data)}
    truth = {row[settings["row_id"]]: float(row[settings["target"]]) for row in data}
    tests = {
        fold: {
            row["row_id"]
            for row in assignments
            if int(row["fold"]) == fold and row["split"] == "test"
        }
        for fold in range(5)
    }
    oof, fold_metrics = [], []
    for (model, fold), file in sorted(predictions.items()):
        rows = read(file) if isinstance(file, Path) else file
        ids = [row["reaction_id"] for row in rows]
        if len(ids) != len(set(ids)) or set(ids) != tests[fold]:
            raise ValueError(f"Invalid test prediction coverage: {model}/{fold}")
        for row in rows:
            identifier = row["reaction_id"]
            if (
                int(row["row_index"]) != indices[identifier]
                or row["target_name"] != settings["target"]
            ):
                raise ValueError("Prediction identity or target differs")
            close(float(row["y_true"]), truth[identifier], 1e-5, "native GT")
            prediction = float(row["y_pred"])
            close(prediction, prediction, 0, "finite prediction")
            oof.append(
                {
                    "model": model,
                    "fold": fold,
                    "path_id": identifier,
                    "row_index": indices[identifier],
                    "y_true": truth[identifier],
                    "reported_y_true": float(row["y_true"]),
                    "y_pred": prediction,
                    "absolute_error": abs(prediction - truth[identifier]),
                }
            )
        selected = [row for row in oof if row["model"] == model and row["fold"] == fold]
        fold_metrics.append(
            {"model": model, "fold": fold, "n_test": len(selected), **metrics(selected)}
        )
    summary = []
    for model in models:
        rows = [row for row in oof if row["model"] == model]
        maes = [row["mae"] for row in fold_metrics if row["model"] == model]
        summary.append(
            {
                "model": model,
                "label": model,
                "folds": 5,
                "n_oof": len(rows),
                "fold_mae_mean": statistics.mean(maes),
                "fold_mae_sd": statistics.stdev(maes),
                **{f"oof_{key}": value for key, value in metrics(rows).items()},
            }
        )
    # Validate the method table before creating the result directory.
    from tricycle_core.training_protocol import training_method

    for (model, _), method_id in bindings.items():
        training_method(protocol_manifest, registry, model, method_id)
    destination = output
    with publication_directory(destination) as output:
        for source, name in [(dataset, "data/dataset.csv"), (splits, "data/splits.csv")]:
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        write_rows(output / "results/oof-predictions.csv", oof)
        write_rows(output / "results/fold-metrics.csv", fold_metrics)
        write_rows(output / "results/model-summary.csv", summary)
        write_method_bindings(output / "METHOD_BINDINGS.csv", bindings, protocol_manifest, registry)
        (output / "manifest.json").write_text(
            json.dumps(
                {
                    "schema": "tricycle.sar.published-fivefold/v1",
                    "target": settings["target"],
                    "unit": "kcal/mol",
                    "models": models,
                    "folds": 5,
                    "rows_per_model": len(data),
                    "dataset_sha256": sha256(dataset),
                    "splits_sha256": sha256(splits),
                    "native_target_tolerance": 1e-5,
                    "metric_tolerance": 2e-12,
                    "backend": {
                        "definition": os.path.relpath(protocol_manifest, output),
                        "sha256": expected_protocol_sha256,
                    },
                },
                indent=2,
            )
            + "\n"
        )
        files = publication_files(output)
        (output / "SHA256SUMS").write_text(
            "".join(f"{sha256(path)}  {path.relative_to(output).as_posix()}\n" for path in files)
        )
        verify(output)
    return destination
