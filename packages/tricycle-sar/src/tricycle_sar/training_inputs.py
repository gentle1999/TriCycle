"""Derive worker inputs from frozen scientific files and one shared method, offline."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import math
import shutil
import tempfile
from pathlib import Path

from tricycle_core.toml import dump_toml, load_toml
from tricycle_core.training_protocol import (
    protocol_file_source,
    resolve_training_registry,
    training_method,
)

from .benchmark import BenchmarkRequest, read_rows, sha256


def native_dataset(rows: list[dict], settings: dict) -> bytes:
    native = []
    for row in rows:
        energy = float(row[settings["reaction_energy_column"]])
        if not math.isfinite(energy) or not math.isfinite(float(row[settings["target"]])):
            raise ValueError("Nonfinite energy or target")
        native.append(
            {
                **row,
                "reaction_id": row[settings["row_id"]],
                "rxn_smiles": row[settings["smiles_column"]],
                "G_T_activate": row[settings["target"]],
                "G_T_rxn": energy,
                "substrate_group": json.dumps([row[c] for c in settings["group_columns"]]),
            }
        )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(native[0]))
    writer.writeheader()
    writer.writerows(native)
    return stream.getvalue().encode()


def validate_splits(
    rows: list[dict], assignments: list[dict], settings: dict, folds: int = 5
) -> None:
    lookup = {row[settings["row_id"]]: row for row in rows}
    if not rows or len(lookup) != len(rows) or any(not key for key in lookup):
        raise ValueError("Empty data or invalid path identity")
    tests = {key: 0 for key in lookup}
    by_fold: dict[int, dict[str, str]] = {fold: {} for fold in range(folds)}
    for row in assignments:
        fold, identifier, role = int(row["fold"]), row["row_id"], row["split"]
        if (
            fold not in by_fold
            or identifier not in lookup
            or role not in {"train", "val", "test"}
            or identifier in by_fold[fold]
        ):
            raise ValueError("Invalid or duplicate split assignment")
        by_fold[fold][identifier] = role
        tests[identifier] += role == "test"
    for split in by_fold.values():
        if split.keys() != lookup.keys():
            raise ValueError("Incomplete fold")
        groups: dict[str, set[tuple[str, ...]]] = {role: set() for role in ("train", "val", "test")}
        for identifier, role in split.items():
            groups[role].add(tuple(lookup[identifier][c] for c in settings["group_columns"]))
        if any(not group for group in groups.values()) or any(
            groups[a] & groups[b] for a, b in (("train", "val"), ("train", "test"), ("val", "test"))
        ):
            raise ValueError("Empty split or substrate-group leakage")
    if any(count != 1 for count in tests.values()):
        raise ValueError("OOF coverage must be exactly once")


def prepare_inputs(
    dataset: Path,
    splits: Path,
    reference: Path,
    model: str,
    fold: int,
    output: Path,
    *,
    method_id: str | None = None,
    assets: Path | None = None,
    retain_checkpoint: bool = False,
) -> dict:
    manifest, registry = resolve_training_registry(reference)
    method_id = method_id or registry["models"][model]["default_method"]
    method = training_method(manifest, registry, model, method_id)
    settings = registry["benchmark_defaults"]["dataset"]
    rows, assignments = read_rows(dataset), read_rows(splits)
    validate_splits(rows, assignments, settings)
    if type(fold) is not int or fold not in range(5):
        raise ValueError("Invalid fold")
    if output.exists():
        raise FileExistsError(f"Refusing to replace worker inputs: {output}")
    content = native_dataset(rows, settings)
    native_hash = hashlib.sha256(content).hexdigest()
    sources, missing = [], []
    for entry in method["files"]:
        if entry["external"]:
            source = assets / entry["sha256"] if assets else None
            if source is None or not source.is_file():
                missing.append(entry["path"])
                continue
            if sha256(source) != entry["sha256"]:
                raise ValueError(f"Fixed asset hash mismatch: {entry['path']}")
        else:
            source = protocol_file_source(manifest, entry)
        # A data-bound native config must agree with this input; never rewrite it.
        if entry["path"].endswith(".toml") and "/configs/tricycle-" in entry["path"]:
            config = load_toml(source)
            if config.get("dataset_sha256", native_hash) != native_hash or config.get(
                "splits_sha256", sha256(splits)
            ) != sha256(splits):
                raise ValueError("Model config differs from the requested data binding")
        sources.append((entry, source))
    if missing:
        raise ValueError(f"Required fixed assets missing: {missing}")
    if importlib.util.find_spec("numpy") is None:
        raise ValueError(
            "Worker input preparation requires local NumPy; use the locked reproduction environment"
        )
    output.mkdir(parents=True)
    for entry, source in sources:
        target = output / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755 if entry["executable"] else 0o644)
    bundle = output / "common" / method["bundle_name"]
    from .modelling_benchmark import native_inputs

    split_settings = registry["benchmark_defaults"]["split"]
    request = BenchmarkRequest(
        dataset,
        sha256(dataset),
        settings["target"],
        settings["row_id"],
        tuple(settings["group_columns"]),
        settings["smiles_column"],
        5,
        int(split_settings["seed"]),
        float(split_settings["validation_fraction"]),
    )
    with tempfile.TemporaryDirectory(prefix="tricycle-worker-data-") as directory:
        data_run = Path(directory)
        shutil.copyfile(dataset, data_run / "dataset.csv")
        shutil.copyfile(splits, data_run / "splits.csv")
        native_inputs(data_run, request, registry["benchmark_defaults"])
        shutil.copytree(data_run / "workspace/data", bundle / "data", dirs_exist_ok=True)
        shutil.copyfile(splits, bundle / "data/splits/ground-truth-5fold/assignments.csv")
    from .backends.registry import render_config

    render_config(bundle, method)
    task = {
        "model": model,
        "fold": fold,
        "smoke": False,
        "environment_model": "classical"
        if method["bundle_name"] == "classical_bundle"
        else "chemprop"
        if model.startswith("chemprop_")
        else model,
    }
    if registry["schema"] in {"tricycle.sar.backends/v1", "tricycle.sar.backends/v2"}:
        task["bundle_name"] = method["bundle_name"]
        if retain_checkpoint:
            task["retain_checkpoint"] = True
    if method["bundle_name"] == "classical_bundle":
        task["models"] = [model]
    elif method["bundle_name"] == "native_bundle":
        environment = "chemprop" if model.startswith("chemprop_") else model
        task.update(
            environment_model=environment,
            source_sha256=sha256(output / f"common/{environment}.tar.gz"),
        )
        task["hashes"] = [
            {"path": p.relative_to(bundle).as_posix(), "sha256": sha256(p)}
            for p in sorted(bundle.rglob("*"))
            if p.is_file()
        ]
    elif method["bundle_name"] == "training_bundle":
        task.update(
            source_sha256=sha256(output / f"common/{model}.tar.gz"),
            benchmark_dataset_sha256=sha256(dataset),
            benchmark_splits_sha256=sha256(splits),
        )
        if model == "rxngraphormer":
            task["conda_sha256"] = sha256(output / "common/micromamba")
        (bundle / "training-bundle.json").write_text(
            json.dumps(
                {
                    "dataset_sha256": sha256(dataset),
                    "splits_sha256": sha256(splits),
                    "files": [
                        {"path": p.relative_to(bundle).as_posix(), "sha256": sha256(p)}
                        for p in sorted(bundle.rglob("*"))
                        if p.is_file()
                    ],
                },
                indent=2,
            )
            + "\n"
        )
    task_path = Path(f"tasks/{model}-fold-{fold}.toml")
    dump_toml(output / task_path, task)
    return {
        "status": "prepared",
        "model": model,
        "fold": fold,
        "method_id": method_id,
        "input_dir": str(output),
        "task": task_path.as_posix(),
        "command": ["bash", method["entrypoint"], task_path.as_posix()],
        "executor": method["executor"],
        "submitted": False,
    }
