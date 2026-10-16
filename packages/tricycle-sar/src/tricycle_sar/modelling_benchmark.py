"""Prepare, validate and publish experiments using the current SAR backends.

TriCycle's run metadata is TOML. JSON/NPZ here are native modelling input
artifacts, not alternative TriCycle configuration formats.
"""

from __future__ import annotations

import importlib
import json
import math
import shutil
import statistics
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tricycle_core.asset_store import resolve_input
from tricycle_core.toml import dump_toml, load_toml

from .benchmark import (
    BenchmarkRequest,
    prepare,
    read_rows,
    regression_metrics,
    sha256,
    validated_rows,
    write_rows,
)
from .molecular_runner import MODELS as MOLECULAR_MODELS


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def validate_models(names: list[str]) -> list[str]:
    from .backends import list_backends

    if not names or len(set(names)) != len(names) or set(names) - set(list_backends()):
        raise ValueError("models must be a nonempty unique subset of the SAR registry")
    return list(names)


def native_inputs(run: Path, request: BenchmarkRequest, value: dict[str, Any]) -> None:
    numpy = importlib.import_module("numpy")
    rows = read_rows(resolve_input(run, "dataset.csv"))
    from .training_inputs import native_dataset

    snapshot = resolve_input(run, "workspace/data/snapshots/ground-truth")
    dataset = snapshot / "dataset.csv"
    snapshot.mkdir(parents=True, exist_ok=True)
    dataset.write_bytes(native_dataset(rows, value["dataset"]))
    native = read_rows(dataset)
    created = datetime.now(UTC).isoformat()
    _json(
        snapshot / "manifest.json",
        {
            "schema_version": "tricycle-snapshot-v1",
            "snapshot_id": run.name,
            "created_at_utc": created,
            "source": {
                "type": "csv",
                "uri": str(request.dataset),
                "sha256": request.expected_sha256,
            },
            "dataset": {
                "format": "canonical-csv-v1",
                "path": "dataset.csv",
                "sha256": sha256(dataset),
            },
            "row_count": len(native),
            "columns": list(native[0]),
            "targets": {
                target: {
                    "count": len(native),
                    "missing": 0,
                    "invalid": 0,
                    "min": min(float(r[target]) for r in native),
                    "max": max(float(r[target]) for r in native),
                    "mean": statistics.mean(float(r[target]) for r in native),
                }
                for target in ("G_T_activate", "G_T_rxn")
            },
            "validation": {"status": "passed", "errors": [], "warnings": []},
        },
    )
    split_root = resolve_input(run, "workspace/data/splits/ground-truth-5fold")
    split_root.mkdir(parents=True)
    assignments = read_rows(resolve_input(run, "splits.csv"))
    row_indices = {r[request.row_id]: i for i, r in enumerate(rows)}
    arrays = {}
    fold_manifests = []
    for fold in range(5):
        files = {}
        for role in ("train", "val", "test"):
            indices = sorted(
                row_indices[r["row_id"]]
                for r in assignments
                if int(r["fold"]) == fold and r["split"] == role
            )
            arrays[f"fold_{fold}_{role}"] = numpy.asarray(indices, dtype=numpy.int64)
            relative = Path(f"fold_{fold}/{role}.indices.csv")
            write_rows(
                split_root / relative,
                [{"row_index": i, "reaction_id": native[i]["reaction_id"]} for i in indices],
            )
            files[role] = {"path": relative.as_posix(), "rows": len(indices)}
        fold_manifests.append({"fold": fold, "files": files})
    numpy.savez(split_root / "indices.npz", **arrays)
    _json(
        split_root / "manifest.json",
        {
            "schema_version": "tricycle-split-v1",
            "split_id": "ground-truth-5fold",
            "created_at_utc": created,
            "snapshot": "data/snapshots/ground-truth",
            "dataset": "data/snapshots/ground-truth/dataset.csv",
            "dataset_sha256": sha256(dataset),
            "row_count": len(rows),
            "strategy": "kfold",
            "parameters": {
                "folds": 5,
                "seed": request.seed,
                "val_ratio": request.validation_fraction,
                "group_column": "substrate_group",
                "target_values_used_for_assignment": False,
                "implementation": "tricycle.sar-benchmark/seeded-greedy-group-5fold-v1",
            },
            "indices": {
                "format": "npz",
                "path": "indices.npz",
                "sha256": sha256(split_root / "indices.npz"),
                "index_base": 0,
                "row_order": "snapshot dataset.csv",
                "arrays": {key: {"rows": len(array)} for key, array in arrays.items()},
            },
            "folds": fold_manifests,
        },
    )


def prepare_modelling(
    config: Path,
    run_dir: Path | None = None,
    *,
    rxngraphormer_assets: Path | None = None,
    reactiont5_assets: Path | None = None,
    molecular_assets: Mapping[str, Path] | None = None,
) -> Path:
    """Create a compact data-bound experiment; methods stay in the shared registry."""
    from tricycle_core.training_protocol import (
        expand_training_config,
        protocol_file_source,
        resolve_training_registry,
        training_method,
    )

    original = load_toml(config)
    if "training_protocol" not in original:
        from .backends.registry import CATALOG

        original["training_protocol"] = {"reference": str(CATALOG)}
    value = expand_training_config(config, original)
    request = BenchmarkRequest.from_mapping(value, config.resolve().parent)
    rows = validated_rows(request)
    count = len({tuple(r[c] for c in request.group_columns) for r in rows})
    for key, actual in [("expected_rows", len(rows)), ("expected_groups", count)]:
        if key in value["dataset"] and int(value["dataset"][key]) != actual:
            raise ValueError(f"{key} mismatch: {actual}")
    manifest, registry = resolve_training_registry(Path(value["training_protocol"]["manifest"]))
    models = validate_models(value["models"])
    methods = {model: training_method(manifest, registry, model) for model in models}
    supplied = {
        **(molecular_assets or {}),
        "reactiont5": reactiont5_assets,
        "rxngraphormer": rxngraphormer_assets,
    }
    assets = []
    for model, method in methods.items():
        prefix = f"common/{method['bundle_name']}/assets/"
        for entry in method["files"]:
            if not entry["path"].startswith(prefix):
                continue
            relative = Path("assets") / entry["path"].removeprefix(prefix)
            if entry["external"]:
                asset_root = supplied.get(model)
                if asset_root is None:
                    continue
                suffix = entry["path"].removeprefix(prefix)
                if model in MOLECULAR_MODELS:
                    suffix = suffix.removeprefix(model + "/")
                asset = asset_root / suffix
                if not asset.is_file() or sha256(asset) != entry["sha256"]:
                    raise ValueError(f"missing or changed fixed {model} asset: {suffix}")
            else:
                asset = protocol_file_source(manifest, entry)
            assets.append((asset, relative))
    run = prepare(config, run_dir, settings=value)
    state = load_toml(run / "run.toml")
    state.update(
        models=models,
        methods={model: registry["models"][model]["default_method"] for model in models},
    )
    dump_toml(run / "run.toml", state)
    native_inputs(run, request, value)
    workspace = run / "workspace"
    # The generated workspace is published to shared storage before returning.
    shutil.copy2(resolve_input(run, "splits.csv"), workspace / "data/splits/ground-truth-5fold/assignments.csv")
    for asset, relative in assets:
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(asset, target)
    if "reactiont5" in models:
        entry = next(
            e
            for e in methods["reactiont5"]["files"]
            if e["path"] == "common/training_bundle/reactiont5.json"
        )
        settings = json.loads(protocol_file_source(manifest, entry).read_text())
        settings["checkpoint_directory"] = "assets"
        _json(workspace / "configs/tricycle-reactiont5.json", settings)
    files = [p for p in workspace.rglob("*") if p.is_file()]
    state["input_hashes"] = [
        {"path": p.relative_to(run).as_posix(), "sha256": sha256(p)}
        for p in sorted(files)
        if p.is_relative_to(workspace / "data") or p.is_relative_to(workspace / "configs")
    ]
    from .protocol_identity import current_identity

    state["method_identities"] = {
        model: current_identity(manifest, methods[model]) for model in models
    }
    dump_toml(run / "run.toml", state)
    check(run)
    from .delivery import freeze

    freeze(run, (workspace,))
    return run


def run_models(run: Path) -> list[str]:
    state = load_toml(run / "run.toml")
    return state["models"]


def check_data(run: Path) -> dict[str, Any]:
    """Audit frozen data/splits independently of changes to executable backends."""
    state = load_toml(run / "run.toml")
    for entry in state["input_hashes"]:
        path = resolve_input(run, entry["path"])
        if not path.is_file() or sha256(path) != entry["sha256"]:
            raise ValueError(f"frozen artifact changed: {entry['path']}")
    for filename, key in (("dataset.csv", "dataset_sha256"), ("splits.csv", "splits_sha256")):
        if state.get(key) and sha256(resolve_input(run, filename)) != state[key]:
            raise ValueError(f"frozen artifact changed: {filename}")
    rows = read_rows(resolve_input(run, "dataset.csv"))
    lookup = {r[state["row_id"]]: r for r in rows}
    splits = read_rows(resolve_input(run, "splits.csv"))
    test_counts: Counter[str] = Counter()
    counts = []
    for fold in range(5):
        part = [r for r in splits if int(r["fold"]) == fold]
        if Counter(r["row_id"] for r in part) != Counter(lookup.keys()):
            raise ValueError("fold does not assign every row exactly once")
        groups: dict[str, set[tuple[str, ...]]] = {r: set() for r in ("train", "val", "test")}
        for assignment in part:
            role = assignment["split"]
            groups[role].add(tuple(lookup[assignment["row_id"]][c] for c in state["group_columns"]))
            if role == "test":
                test_counts[assignment["row_id"]] += 1
        if any(not group for group in groups.values()) or any(
            groups[a] & groups[b] for a, b in (("train", "val"), ("train", "test"), ("val", "test"))
        ):
            raise ValueError("empty split or substrate-group leakage")
        counts.append({"fold": fold, **dict(Counter(r["split"] for r in part))})
    if test_counts != Counter(lookup.keys()):
        raise ValueError("OOF test coverage is not exactly once")
    return {"rows": len(rows), "fold_counts": counts}


def check(run: Path) -> dict[str, Any]:
    """Audit source/input identity and current executable methods before training."""
    data = check_data(run)
    state = load_toml(run / "run.toml")
    molecular_missing = {}
    from tricycle_core.training_protocol import resolve_training_registry, training_method

    manifest, registry = resolve_training_registry(
        Path(state["training_protocol_manifest"]), state["training_protocol_manifest_sha256"]
    )
    for name in run_models(run):
        method = training_method(manifest, registry, name, state["methods"][name])
        if "method_identities" in state:
            from .protocol_identity import current_identity

            if current_identity(manifest, method) != state["method_identities"][name]:
                raise ValueError("Recorded scoped method identity differs")
        prefix = f"common/{method['bundle_name']}/"
        unavailable = []
        for entry in method["files"]:
            if not entry["external"] or not entry["path"].startswith(prefix):
                continue
            asset = resolve_input(run, "workspace") / entry["path"].removeprefix(prefix)
            if not asset.is_file():
                unavailable.append(str(asset))
            elif sha256(asset) != entry["sha256"]:
                raise ValueError(f"fixed model asset changed: {entry['path']}")
        molecular_missing[name] = unavailable
    result = {
        "missing_molecular_assets": molecular_missing,
        "missing_reactiont5_assets": molecular_missing.get("reactiont5", []),
        "status": "local-input-check-passed",
        **data,
        "missing_rxngraphormer_assets": molecular_missing.get("rxngraphormer", []),
        "runtime_status": "not_checked; Docker images/GPU/weights require execution-host preflight",
        "training_started": any(
            prediction_file(run, model, fold).parent.exists()
            for model in run_models(run)
            for fold in range(5)
        ),
    }
    return result


def prediction_file(run: Path, model: str, fold: int) -> Path:
    if model in {"rxnresid", "rxngraphormer", "reactiont5"}:
        return run / f"workspace/model-runs/{model}/fold_{fold}/predictions.csv"
    root = f"model-runs/{model}"
    return run / f"workspace/{root}/{model}/fold_{fold}/predictions.csv"


def compact_predictions(directory: Path) -> dict | None:
    """Read compact worker tables directly, without materializing fold files."""
    paths = sorted(directory.glob("**/worker-*/results/predictions.csv"))
    for path in (directory / "predictions.csv", directory / "results/predictions.csv"):
        if path.is_file():
            paths.append(path)
    if not paths:
        return None
    groups: dict[tuple[str, int], list[dict[str, str]]] = {}
    for path in dict.fromkeys(paths):
        for row in read_rows(path):
            groups.setdefault((row["model"], int(row["fold"])), []).append(row)
    return groups


def collect(run: Path, *, prediction_dir: Path | None = None) -> dict[str, Any]:
    """Fail closed on incomplete, duplicate, stale, or misaligned OOF predictions."""
    check_data(run)
    state = load_toml(run / "run.toml")
    source = {"models": run_models(run)}
    rows = read_rows(resolve_input(run, "dataset.csv"))
    splits = read_rows(resolve_input(run, "splits.csv"))
    summary = {}
    oof = []
    compact = compact_predictions(prediction_dir or run)
    expected_models = {(model, fold) for model in source["models"] for fold in range(5)}
    if compact is not None and compact.keys() != expected_models:
        raise ValueError("Compact worker tables do not cover the experiment models and folds")
    for model in source["models"]:
        metrics = []
        pooled_y: list[float] = []
        pooled_p: list[float] = []
        for fold in range(5):
            file = prediction_file(run, model, fold)
            predictions = read_rows(file) if compact is None else compact[model, fold]
            if model == "reamvp":
                proof = json.loads(file.with_name("run_manifest.json").read_text())
                command = proof["command"]
                if (
                    proof["dataset_sha256"]
                    != sha256(resolve_input(run, "workspace/data/snapshots/ground-truth/dataset.csv"))
                    or proof["split_sha256"]
                    != sha256(resolve_input(run, "workspace/data/splits/ground-truth-5fold/manifest.json"))
                    or proof["fold"] != fold
                    or proof["evaluation_split"] != "test"
                    or command[command.index("--target") + 1] != state["target"]
                ):
                    raise ValueError("ReaMVP strict-run provenance mismatch")
                predictions = [{**p, "target_name": state["target"]} for p in predictions]
            expected = {
                r["row_id"] for r in splits if int(r["fold"]) == fold and r["split"] == "test"
            }
            if Counter(r["reaction_id"] for r in predictions) != Counter(expected):
                raise ValueError(f"invalid OOF coverage: {model} fold {fold}")
            yy, pp = [], []
            for pred in predictions:
                idx = int(pred["row_index"])
                if not 0 <= idx < len(rows):
                    raise ValueError("prediction row index out of range")
                row = rows[idx]
                y, prediction = float(row[state["target"]]), float(pred["y_pred"])
                # Some GPU runners serialize this redundant label through a
                # float32 tensor. The frozen dataset remains authoritative for
                # metrics, so allow only the expected float32 rounding error.
                if (
                    row[state["row_id"]] != pred["reaction_id"]
                    or pred["target_name"] != state["target"]
                    or not math.isclose(y, float(pred["y_true"]), abs_tol=5e-6, rel_tol=0)
                ):
                    raise ValueError("prediction identity/target mismatch")
                yy.append(y)
                pp.append(prediction)
                oof.append({"model": model, "fold": fold, **pred})
            metrics.append(regression_metrics(yy, pp))
            pooled_y.extend(yy)
            pooled_p.extend(pp)
        summary[model] = {
            "pooled": regression_metrics(pooled_y, pooled_p),
            "fold_mae_mean": statistics.mean(float(m["mae"]) for m in metrics),
            "fold_mae_std": statistics.stdev(float(m["mae"]) for m in metrics),
            "folds": metrics,
        }
    if not state.get("methods"):
        # Retain the old collection contract only for existing unbound runs.
        write_rows(run / "oof-predictions.csv", oof)
        dump_toml(
            run / "metrics.toml", {"dataset_sha256": state["dataset_sha256"], "models": summary}
        )
    return summary


def publish_results(run: Path, output: Path, *, prediction_dir: Path | None = None) -> Path:
    """Publish only scientific files; raw execution evidence remains in the run."""
    from .results import publish

    collect(run, prediction_dir=prediction_dir)
    state = load_toml(run / "run.toml")
    if not state.get("methods"):
        raise ValueError("Publishing needs explicit frozen method bindings")
    bindings = {
        (model, fold): state["methods"][model] for model in run_models(run) for fold in range(5)
    }
    for row in state.get("method_overrides", []):
        key = row["model"], row["fold"]
        if key not in bindings:
            raise ValueError("Unknown method override")
        bindings[key] = row["method_id"]
    return publish(
        resolve_input(run, "dataset.csv"),
        resolve_input(run, "splits.csv"),
        compact_predictions(prediction_dir or run)
        or {key: prediction_file(run, *key) for key in bindings},
        bindings,
        Path(state["training_protocol_manifest"]),
        output,
        expected_protocol_sha256=state["training_protocol_manifest_sha256"],
    )
