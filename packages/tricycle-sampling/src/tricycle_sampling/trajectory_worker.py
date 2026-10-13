#!/usr/bin/env python3
"""Run one complete RxnResid active-learning curve inside one Bohr task.

The input bundle is prepared by ``rxnresid_active_learning.py``.  Each round
selects exactly 40 substrate pairs, adds every project reaction row belonging
to those pairs to the training pool, fits from the previous round's checkpoint
when one exists, predicts the complete ground-truth space, and records row-level
MAE plus the materialized sampling cost. The trajectory seed controls only the
first random batch. Later acquisition ties use candidate IDs, random-baseline
ranking uses a fixed hash order, and model training uses ``fixed_seed``.
A pair with no project label is kept in the selected set and contributes its
cost; it simply contributes zero rows to the training pool.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
import tomllib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tricycle_core.io import write_delimited
from tricycle_core.io import write_json as _write_json
from tricycle_core.metrics import regression_metrics
from tricycle_sar import get_backend
from tricycle_sar import rxnresid as sar_rxnresid
from tricycle_sar.predictions import aggregate_pairs, validate_rows

from tricycle_sampling import Candidate, SamplingPlugin, SamplingRequest
from tricycle_sampling.experiment import (
    EXPERIMENT_SAMPLING_SCHEMA,
    reactant_structure_features,
    select_experiment_candidates,
)
from tricycle_sampling.protocols import LITERATURE_BASELINE_STRATEGIES

ROOT = Path.cwd().resolve()

STRATEGIES = {
    "random",
    "predictive-uncertainty",
    "epistemic-uncertainty",
    "aleatoric-uncertainty",
    "pair-core-set",
    "density-weighted-uncertainty",
    "uncertainty-diversity",
    *LITERATURE_BASELINE_STRATEGIES,
}
MODEL_SCORE_STRATEGIES = {
    "predictive-uncertainty",
    "epistemic-uncertainty",
    "aleatoric-uncertainty",
    "density-weighted-uncertainty",
    "uncertainty-diversity",
    "ucb",
    "distance-uncertainty-product",
    "uncertainty-kriging-believer",
}
NATIVE_COLUMNS = (
    "path_id",
    "source_mapped_reaction_id",
    "source_row_index",
    "rxn_smiles",
    "G_T_activate",
    "ene_id",
    "diene_id",
    "prod_id",
)
SCORE_COLUMNS = (
    "predictive_variance",
    "epistemic_variance",
    "aleatoric_variance",
)
UNCERTAINTY_AGGREGATIONS = {"sum"}
DEFAULT_FIXED_SEED = 42


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"CSV file not found: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return [dict(row) for row in reader]


def _write_csv(path: Path, fieldnames: list[str], rows: Sequence[Mapping[str, object]]) -> None:
    write_delimited(path, fieldnames, rows, project_fields=False)


def _resolve_path(value: object, *, base: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return (path if path.is_absolute() else base / path).resolve()


def _finite_float(value: object, *, field: str, path: Path) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: non-numeric {field}: {value!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{path}: non-finite {field}")
    return parsed


def _native_rows(path: Path) -> list[dict[str, str]]:
    rows = _read_csv(path)
    if rows:
        missing = [column for column in NATIVE_COLUMNS if column not in rows[0]]
        if missing:
            raise ValueError(f"{path}: missing native columns: {', '.join(missing)}")
    seen: set[str] = set()
    for row_index, row in enumerate(rows):
        for column in NATIVE_COLUMNS:
            if not row.get(column, "").strip():
                raise ValueError(f"{path}: row {row_index} has empty {column!r}")
        path_id = row["path_id"].strip()
        if path_id in seen:
            raise ValueError(f"{path}: duplicate path_id {path_id!r}")
        seen.add(path_id)
        _finite_float(row["G_T_activate"], field="G_T_activate", path=path)
        try:
            route_id = int(row["prod_id"])
        except ValueError as exc:
            raise ValueError(f"{path}: row {row_index} has invalid prod_id") from exc
        if route_id < 0:
            raise ValueError(f"{path}: row {row_index} has negative prod_id")
    return rows


def _load_trajectory(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        request = tomllib.load(handle)
    if request.get("schema") != "tricycle.rxnresid-active-learning-trajectory/v1":
        raise ValueError(f"unsupported trajectory schema: {path}")
    strategy = str(request.get("strategy", ""))
    if strategy not in STRATEGIES:
        raise ValueError(f"unsupported active-learning strategy: {strategy}")
    request["seed"] = int(request.get("seed", 0))
    request["fixed_seed"] = int(request.get("fixed_seed", DEFAULT_FIXED_SEED))
    if request["fixed_seed"] < 0:
        raise ValueError("fixed_seed must be non-negative")
    uncertainty_aggregation = str(request.get("uncertainty_aggregation", "sum")).strip().lower()
    if uncertainty_aggregation not in UNCERTAINTY_AGGREGATIONS:
        raise ValueError(
            "uncertainty_aggregation must be 'sum' so pair acquisition follows the path-level MAE"
        )
    # Keep older trajectory files runnable while making the effective rule
    # explicit in every downstream audit file.
    request["uncertainty_aggregation"] = uncertainty_aggregation
    diversity_weight = float(request.get("diversity_weight", 0.5))
    if not math.isfinite(diversity_weight) or not 0.0 <= diversity_weight <= 1.0:
        raise ValueError("diversity_weight must be a finite number in [0, 1]")
    request["diversity_weight"] = diversity_weight
    pair_batch_size = int(request.get("pair_batch_size", 0))
    round_count = int(request.get("round_count", 0))
    if pair_batch_size < 1 or round_count < 1:
        raise ValueError("pair_batch_size and round_count must be positive")
    return request


def _load_pair_pool(path: Path) -> list[dict[str, str]]:
    rows = _read_csv(path)
    required = (
        "pair_id",
        "pair_order",
        "ene_id",
        "diene_id",
        "project_label_row_count",
        "materialized_sampling_cost_core_hours",
    )
    if rows:
        missing = [column for column in required if column not in rows[0]]
        if missing:
            raise ValueError(f"{path}: missing pair-pool columns: {', '.join(missing)}")
    seen: set[str] = set()
    for row_index, row in enumerate(rows):
        pair = row["pair_id"].strip()
        expected = f"{row['ene_id'].strip()}+{row['diene_id'].strip()}"
        if pair != expected:
            raise ValueError(f"{path}: pair_id mismatch at row {row_index}")
        if pair in seen:
            raise ValueError(f"{path}: duplicate pair_id {pair!r}")
        seen.add(pair)
        if int(row["pair_order"]) != row_index:
            raise ValueError(f"{path}: pair_order must be contiguous and sorted")
        cost = _finite_float(
            row["materialized_sampling_cost_core_hours"],
            field="materialized_sampling_cost_core_hours",
            path=path,
        )
        if cost < 0:
            raise ValueError(f"{path}: negative sampling cost at row {row_index}")
    return rows


def _rows_by_pair(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[f"{row['ene_id']}+{row['diene_id']}"].append(row)
    return dict(grouped)


def _load_base_config() -> dict[str, Any]:
    return sar_rxnresid.load_base_config(ROOT / "rxnresid")


_write_round_config = sar_rxnresid.write_round_config


def _write_all_train_manifest(training_csv: Path, manifest_path: Path) -> int:
    vocabulary = ROOT / "route-id-vocabulary.json"
    return sar_rxnresid.write_all_train_manifest(
        training_csv, manifest_path, route_vocabulary=vocabulary if vocabulary.is_file() else None
    )


_prepare_refit_checkpoint = sar_rxnresid.prepare_refit_checkpoint


def _run_logged(command: list[str], *, cwd: Path, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(command) + "\n\n")
        log.flush()
        completed = subprocess.run(
            command,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
            env=os.environ.copy(),
        )
    if completed.returncode != 0:
        raise RuntimeError(f"command failed with exit code {completed.returncode}; see {log_path}")


def _parse_prediction_output(
    path: Path,
    *,
    expected_rows: int,
    expected_pairs: set[str],
    expected: Sequence[Mapping[str, str]],
) -> tuple[float, dict[str, dict[str, float]], list[dict[str, str]]]:
    rows = _read_csv(path)
    required = (
        "ene_id",
        "diene_id",
        "prediction",
        "target",
        *SCORE_COLUMNS,
    )
    if rows:
        missing = [column for column in required if column not in rows[0]]
        if missing:
            raise ValueError(f"{path}: prediction is missing columns: {', '.join(missing)}")
    if len(rows) != expected_rows:
        raise ValueError(f"{path}: expected {expected_rows} rows, got {len(rows)}")
    validate_rows(rows, expected=expected, required=("prediction", "target", *SCORE_COLUMNS))
    pair_scores = aggregate_pairs(rows)
    if set(pair_scores) != expected_pairs:
        raise ValueError(f"{path}: prediction pair coverage differs from expected space")
    for values in pair_scores.values():
        values["ground_truth_path_count"] = values["product_count"]
    mae = regression_metrics(
        [float(r["target"]) for r in rows], [float(r["prediction"]) for r in rows]
    )["mae"]
    return float(mae), pair_scores, rows


def _candidate(row: Mapping[str, str], scores: Mapping[str, float] | None = None) -> Candidate:
    payload: dict[str, object] = {
        "ene_id": row["ene_id"],
        "diene_id": row["diene_id"],
    }
    if row.get("pair_embedding", "").strip():
        payload["pair_embedding"] = int(row["pair_embedding"])
    if row.get("pair_density", "").strip():
        payload["pair_density"] = float(row["pair_density"])
    if scores is not None:
        payload.update(scores)
    return Candidate(candidate_id=row["pair_id"], payload=payload)


def _select(
    strategy: str,
    rows: list[dict[str, str]],
    *,
    budget: int,
    seed: int | None,
    scores: Mapping[str, Mapping[str, float]] | None,
    reference_rows: Sequence[Mapping[str, str]] = (),
    diversity_weight: float = 0.5,
    matched: bool = False,
    design_size: int = 0,
    offset: int = 0,
    decision_path: Path | None = None,
) -> tuple[list[dict[str, str]], str]:
    if matched and strategy in LITERATURE_BASELINE_STRATEGIES:

        def features(row: Mapping[str, str]) -> tuple[float, ...]:
            return reactant_structure_features(row["ene_smiles"] + "." + row["diene_smiles"])

        if scores is None or any(row["pair_id"] not in scores for row in rows):
            raise ValueError(
                "matched sampling requires the shared model predictions; no random fallback"
            )
        matched_candidates = tuple(
            Candidate(
                row["pair_id"], {**scores[row["pair_id"]], "design_features": list(features(row))}
            )
            for row in rows
        )
        result = select_experiment_candidates(
            strategy,
            matched_candidates,
            reference_features={row["pair_id"]: features(row) for row in reference_rows},
            budget=budget,
            seed=seed if seed is not None else DEFAULT_FIXED_SEED,
            design_size=design_size,
            offset=offset,
        )
        if decision_path is not None:
            decision_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n")
        rows_by_id = {row["pair_id"]: row for row in rows}
        return [rows_by_id[key] for key in result.selected_ids], strategy
    plugin = SamplingPlugin()
    ordered_rows = sorted(rows, key=lambda row: row["pair_id"])
    selection_strategy = strategy
    needs_scores = strategy in MODEL_SCORE_STRATEGIES
    has_scores = scores is not None and all(row["pair_id"] in scores for row in ordered_rows)
    if needs_scores and not has_scores:
        selection_strategy = "random-no-model"
        strategy_for_plugin = "random"
    else:
        strategy_for_plugin = strategy
    candidates = [
        _candidate(
            row,
            {
                "predictive_variance": scores[row["pair_id"]]["predictive_variance"],
                "epistemic_variance": scores[row["pair_id"]]["epistemic_variance"],
                "aleatoric_variance": scores[row["pair_id"]]["aleatoric_variance"],
            }
            if scores is not None and has_scores and strategy != "random"
            else None,
        )
        for row in ordered_rows
    ]
    metadata: dict[str, Any] = {}
    if strategy in {"pair-core-set", "uncertainty-diversity"}:
        metadata = {
            "reference_embeddings": {
                row["pair_id"]: int(row["pair_embedding"]) for row in reference_rows
            },
            "diversity_weight": diversity_weight,
        }
    selected = plugin.select(
        SamplingRequest(
            candidates=tuple(candidates),
            budget=budget,
            strategy=strategy_for_plugin,
            seed=seed,
            metadata=metadata,
        )
    ).selected
    rows_by_id = {row["pair_id"]: row for row in ordered_rows}
    return [rows_by_id[candidate.candidate_id] for candidate in selected], selection_strategy


def _select_from_fixed_hash_order(
    available: list[dict[str, str]],
    *,
    budget: int,
    fixed_seed: int,
) -> list[dict[str, str]]:
    """Select the lowest fixed hash ranks, independent of input row order."""

    def rank(row: Mapping[str, str]) -> tuple[bytes, str]:
        pair_id = str(row["pair_id"])
        material = f"tricycle-random-acquisition-v1\0{fixed_seed}\0{pair_id}"
        return hashlib.sha256(material.encode("utf-8")).digest(), pair_id

    if budget < 0 or budget > len(available):
        raise ValueError("sampling budget must fit the available candidate count")
    return sorted(available, key=rank)[:budget]


def _round_row(
    *,
    trajectory: Mapping[str, Any],
    round_index: int,
    selection_seed: int | None,
    selection_method: str,
    selected: list[dict[str, str]],
    total_selected: list[dict[str, str]],
    newly_labeled_pairs: int,
    newly_unlabeled_pairs: int,
    new_label_rows: int,
    total_label_rows: int,
    training_status: str,
    training_initialization: str,
    inference_status: str,
    full_space_row_count: int,
    mae: float | None,
    round_cost: float,
    cumulative_cost: float,
) -> dict[str, object]:
    return {
        "trajectory_id": trajectory["trajectory_id"],
        "project": trajectory["project"],
        "strategy": trajectory["strategy"],
        "uncertainty_aggregation": trajectory["uncertainty_aggregation"],
        "seed": trajectory["seed"],
        "fixed_seed": trajectory["fixed_seed"],
        "round_index": round_index,
        "selection_seed": selection_seed,
        "selection_method": selection_method,
        "new_pair_count": len(selected),
        "total_selected_pairs": len(total_selected),
        "remaining_pair_count": int(trajectory["pair_count"]) - len(total_selected),
        "new_labeled_pair_count": newly_labeled_pairs,
        "new_unlabeled_pair_count": newly_unlabeled_pairs,
        "new_reaction_label_rows": new_label_rows,
        "total_training_reaction_rows": total_label_rows,
        "training_status": training_status,
        "training_initialization": training_initialization,
        "inference_status": inference_status,
        "full_space_row_count": full_space_row_count,
        "full_space_mae_kcal_mol": "" if mae is None else format(mae, ".12g"),
        "round_sampling_cost_core_hours": format(round_cost, ".12g"),
        "cumulative_sampling_cost_core_hours": format(cumulative_cost, ".12g"),
        "cost_column": trajectory["cost_column"],
        "cost_unit": trajectory["cost_unit"],
    }


def run_trajectory(request_path: Path) -> dict[str, object]:
    trajectory = _load_trajectory(request_path)
    # The normal staged layout places this file below <bundle>/trajectories/<id>.
    bundle_root = ROOT
    from tricycle_sar.sampling_runtime import verify as verify_sar_runtime

    sar_backend = verify_sar_runtime(bundle_root)
    if (
        trajectory.get("source_revision", sar_backend["source_revision"])
        != sar_backend["source_revision"]
    ):
        raise ValueError("trajectory source revision differs from SAR backend")
    backend = get_backend("rxnresid")
    ground_truth_path = _resolve_path(trajectory["ground_truth"], base=bundle_root)
    project_labels_path = _resolve_path(trajectory["project_labels"], base=bundle_root)
    pair_pool_path = _resolve_path(trajectory["pair_pool"], base=bundle_root)
    output_dir = _resolve_path(trajectory["output_dir"], base=bundle_root)
    output_dir.mkdir(parents=True, exist_ok=True)

    ground_truth = _native_rows(ground_truth_path)
    project_labels = _native_rows(project_labels_path)
    pair_pool = _load_pair_pool(pair_pool_path)
    if len(pair_pool) != int(trajectory["pair_batch_size"]) * int(trajectory["round_count"]):
        raise ValueError("pair pool size does not match pair_batch_size * round_count")
    expected_pairs = {row["pair_id"] for row in pair_pool}
    ground_truth_pairs = {f"{row['ene_id']}+{row['diene_id']}" for row in ground_truth}
    if ground_truth_pairs != expected_pairs:
        raise ValueError("ground-truth pair space does not match the candidate pool")
    project_by_pair = _rows_by_pair(project_labels)
    for pair in project_by_pair:
        if pair not in expected_pairs:
            raise ValueError(f"project label pair is outside candidate pool: {pair}")

    _write_csv(
        output_dir / "pair-pool-snapshot.csv",
        list(pair_pool[0]),
        pair_pool,
    )
    _write_json(
        output_dir / "trajectory.json",
        {
            **trajectory,
            "ground_truth_rows": len(ground_truth),
            "ground_truth_pairs": len(ground_truth_pairs),
            "project_label_rows": len(project_labels),
            "project_labeled_pairs": len(project_by_pair),
            "sampling_cost_rule": "direct materialized core-hours; missing values are zero",
            "uncertainty_aggregation": trajectory["uncertainty_aggregation"],
        },
    )

    base_config = _load_base_config()
    python_bin = os.environ.get("TRICYCLE_PYTHON", sys.executable)
    vocabulary = ROOT / "route-id-vocabulary.json"
    native_options = {
        "python_executable": python_bin,
        "source_directory": ROOT / "rxnresid",
        "route_vocabulary": vocabulary if vocabulary.is_file() else None,
    }
    selected_ids: set[str] = set()
    selected_rows: list[dict[str, str]] = []
    score_map: dict[str, dict[str, float]] | None = None
    checkpoint: Path | None = None
    curve_rows: list[dict[str, object]] = []
    decision_rows: list[dict[str, object]] = []
    prediction_audit_rows: list[dict[str, str]] = []
    cumulative_cost = 0.0
    curve_fields = [
        "trajectory_id",
        "project",
        "strategy",
        "uncertainty_aggregation",
        "seed",
        "fixed_seed",
        "round_index",
        "selection_seed",
        "selection_method",
        "new_pair_count",
        "total_selected_pairs",
        "remaining_pair_count",
        "new_labeled_pair_count",
        "new_unlabeled_pair_count",
        "new_reaction_label_rows",
        "total_training_reaction_rows",
        "training_status",
        "training_initialization",
        "inference_status",
        "full_space_row_count",
        "full_space_mae_kcal_mol",
        "round_sampling_cost_core_hours",
        "cumulative_sampling_cost_core_hours",
        "cost_column",
        "cost_unit",
    ]
    decision_fields = [
        "trajectory_id",
        "round_index",
        "within_round_index",
        "pair_id",
        "ene_id",
        "diene_id",
        "project_label_row_count",
        "has_valid_training_label",
        "materialized_sampling_cost_core_hours",
        "cost_was_missing",
        "selection_method",
        "selection_seed",
    ]
    score_fields = [
        "pair_id",
        "ene_id",
        "diene_id",
        "available_before_selection",
        "ground_truth_path_count",
        "uncertainty_aggregation",
        "predictive_variance",
        "epistemic_variance",
        "aleatoric_variance",
    ]

    batch_size = int(trajectory["pair_batch_size"])
    round_count = int(trajectory["round_count"])
    seed = int(trajectory["seed"])
    fixed_seed = int(trajectory["fixed_seed"])
    strategy = str(trajectory["strategy"])
    for round_index in range(round_count):
        round_dir = output_dir / f"round-{round_index:04d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        available = [row for row in pair_pool if row["pair_id"] not in selected_ids]
        if round_index == 0:
            selection_seed: int | None = seed
            chosen, selection_method = _select(
                "random",
                available,
                budget=batch_size,
                seed=selection_seed,
                scores=score_map,
            )
        else:
            needs_scores = strategy in MODEL_SCORE_STRATEGIES
            has_scores = score_map is not None and all(
                row["pair_id"] in score_map for row in available
            )
            matched = (
                trajectory.get("sampling_experiment") == EXPERIMENT_SAMPLING_SCHEMA
                and strategy in LITERATURE_BASELINE_STRATEGIES
            )
            if strategy == "random" or (needs_scores and not has_scores and not matched):
                selection_seed = fixed_seed
                selection_method = "random" if strategy == "random" else "random-no-model"
                chosen = _select_from_fixed_hash_order(
                    available,
                    budget=batch_size,
                    fixed_seed=fixed_seed,
                )
            else:
                selection_seed = fixed_seed if matched else None
                chosen, selection_method = _select(
                    strategy,
                    available,
                    budget=batch_size,
                    seed=fixed_seed if matched else None,
                    scores=score_map,
                    reference_rows=[
                        row for row in selected_rows if project_by_pair.get(row["pair_id"])
                    ]
                    if matched
                    else selected_rows,
                    diversity_weight=float(trajectory.get("diversity_weight", 0.5)),
                    matched=matched,
                    design_size=len(pair_pool) - batch_size,
                    offset=len(selected_rows) - batch_size,
                    decision_path=round_dir / "sampling-decision.json" if matched else None,
                )
        if len(chosen) != batch_size:
            raise RuntimeError(
                f"round {round_index} selected {len(chosen)} pairs, expected {batch_size}"
            )
        selected_ids.update(row["pair_id"] for row in chosen)
        selected_rows.extend(chosen)
        label_rows = [
            row
            for selected in selected_rows
            for row in project_by_pair.get(selected["pair_id"], ())
        ]
        new_label_rows = sum(len(project_by_pair.get(row["pair_id"], ())) for row in chosen)
        newly_labeled_pairs = sum(bool(project_by_pair.get(row["pair_id"])) for row in chosen)
        newly_unlabeled_pairs = len(chosen) - newly_labeled_pairs
        round_cost = sum(float(row["materialized_sampling_cost_core_hours"]) for row in chosen)
        cumulative_cost += round_cost

        round_dir = output_dir / f"round-{round_index:04d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        _write_csv(round_dir / "selected-pairs.csv", list(chosen[0]), chosen)
        _write_csv(round_dir / "training-pool.csv", list(NATIVE_COLUMNS), label_rows)
        training_status = "skipped-no-valid-labels"
        training_initialization = "none"
        inference_status = "skipped-no-model"
        mae: float | None = None
        full_predictions: list[dict[str, str]] = []
        score_map = None
        if label_rows and (checkpoint is None or new_label_rows > 0):
            manifest_path = round_dir / "split-all-selected.json"
            _write_all_train_manifest(round_dir / "training-pool.csv", manifest_path)
            config_path = round_dir / "rxnresid.yaml"
            _write_round_config(
                config_path,
                base_config,
                training_csv=round_dir / "training-pool.csv",
                split_manifest=manifest_path,
                output_dir=round_dir,
                epochs=int(trajectory["training_epochs"]),
                batch_size_paths=int(trajectory["training_batch_size_paths"]),
                device=str(trajectory["training_device"]),
                mixed_precision=str(trajectory["training_mixed_precision"]),
                seed=fixed_seed,
            )
            train_output = round_dir / "train-output"
            train_options = {
                **native_options,
                "output": str(train_output),
                "epochs": int(trajectory["training_epochs"]),
                "batch_size": int(trajectory["training_batch_size_paths"]),
                "device": str(trajectory["training_device"]),
                "mixed_precision": str(trajectory["training_mixed_precision"]),
            }
            if checkpoint is None:
                train_command = list(
                    backend.training_command(
                        config=str(config_path),
                        fold_index=0,
                        train_on_train_valid_from_scratch=True,
                        seed=fixed_seed,
                        **train_options,
                    )
                )
                _run_logged(
                    train_command,
                    cwd=ROOT / "rxnresid",
                    log_path=round_dir / "train.log",
                )
                training_initialization = "from-scratch"
            else:
                refit_checkpoint = round_dir / ".previous-round-checkpoint.pt"
                _prepare_refit_checkpoint(
                    checkpoint,
                    refit_checkpoint,
                    training_csv=round_dir / "training-pool.csv",
                    split_manifest=manifest_path,
                    fixed_seed=fixed_seed,
                )
                train_command = list(
                    backend.refit_command(checkpoint=str(refit_checkpoint), **train_options)
                )
                try:
                    _run_logged(
                        train_command,
                        cwd=ROOT / "rxnresid",
                        log_path=round_dir / "train.log",
                    )
                finally:
                    refit_checkpoint.unlink(missing_ok=True)
                training_initialization = "previous-round-checkpoint"
            checkpoint = train_output / "checkpoint.pt"
            if not checkpoint.is_file():
                raise RuntimeError(f"RxnResid training did not write checkpoint: {checkpoint}")
            training_status = "trained-all-selected-labels"
            prediction_path = round_dir / "full-space-predictions.csv"
            predict_command = list(
                backend.inference_command(
                    checkpoint=str(checkpoint),
                    data=str(ground_truth_path),
                    output=str(prediction_path),
                    batch_size=int(trajectory["training_batch_size_paths"]),
                    component_id_columns=("ene_id", "diene_id"),
                    cpu=str(trajectory["training_device"]) == "cpu",
                    **native_options,
                )
            )
            _run_logged(
                predict_command,
                cwd=ROOT / "rxnresid",
                log_path=round_dir / "infer.log",
            )
            mae, score_map, full_predictions = _parse_prediction_output(
                prediction_path,
                expected_rows=len(ground_truth),
                expected_pairs=expected_pairs,
                expected=ground_truth,
            )
            inference_status = "predicted-ground-truth-space"
            if bool(trajectory.get("persist_round_predictions", True)):
                prediction_audit_rows.extend(
                    {
                        "trajectory_id": trajectory["trajectory_id"],
                        "round_index": str(round_index),
                        **row,
                    }
                    for row in full_predictions
                )
        elif checkpoint is not None:
            training_initialization = "previous-checkpoint-reused"
            prediction_path = round_dir / "full-space-predictions.csv"
            predict_command = list(
                backend.inference_command(
                    checkpoint=str(checkpoint),
                    data=str(ground_truth_path),
                    output=str(prediction_path),
                    batch_size=int(trajectory["training_batch_size_paths"]),
                    component_id_columns=("ene_id", "diene_id"),
                    cpu=str(trajectory["training_device"]) == "cpu",
                    **native_options,
                )
            )
            _run_logged(
                predict_command,
                cwd=ROOT / "rxnresid",
                log_path=round_dir / "infer.log",
            )
            mae, score_map, full_predictions = _parse_prediction_output(
                prediction_path,
                expected_rows=len(ground_truth),
                expected_pairs=expected_pairs,
                expected=ground_truth,
            )
            training_status = "reused-previous-checkpoint-no-new-labels"
            inference_status = "predicted-ground-truth-space"
            if bool(trajectory.get("persist_round_predictions", True)):
                prediction_audit_rows.extend(
                    {
                        "trajectory_id": trajectory["trajectory_id"],
                        "round_index": str(round_index),
                        **row,
                    }
                    for row in full_predictions
                )

        for within_round_index, row in enumerate(chosen):
            decision_rows.append(
                {
                    "trajectory_id": trajectory["trajectory_id"],
                    "round_index": round_index,
                    "within_round_index": within_round_index,
                    "pair_id": row["pair_id"],
                    "ene_id": row["ene_id"],
                    "diene_id": row["diene_id"],
                    "project_label_row_count": row["project_label_row_count"],
                    "has_valid_training_label": row["has_valid_training_label"],
                    "materialized_sampling_cost_core_hours": row[
                        "materialized_sampling_cost_core_hours"
                    ],
                    "cost_was_missing": row["cost_was_missing"],
                    "selection_method": selection_method,
                    "selection_seed": selection_seed,
                }
            )
        _write_csv(output_dir / "selection-decisions.csv", decision_fields, decision_rows)
        curve_rows.append(
            _round_row(
                trajectory=trajectory,
                round_index=round_index,
                selection_seed=selection_seed,
                selection_method=selection_method,
                selected=chosen,
                total_selected=selected_rows,
                newly_labeled_pairs=newly_labeled_pairs,
                newly_unlabeled_pairs=newly_unlabeled_pairs,
                new_label_rows=new_label_rows,
                total_label_rows=len(label_rows),
                training_status=training_status,
                training_initialization=training_initialization,
                inference_status=inference_status,
                full_space_row_count=len(ground_truth),
                mae=mae,
                round_cost=round_cost,
                cumulative_cost=cumulative_cost,
            )
        )
        _write_csv(output_dir / "curve.csv", curve_fields, curve_rows)
        if score_map is not None:
            score_rows = []
            for row in pair_pool:
                scores = score_map[row["pair_id"]]
                score_rows.append(
                    {
                        "pair_id": row["pair_id"],
                        "ene_id": row["ene_id"],
                        "diene_id": row["diene_id"],
                        "available_before_selection": row["pair_id"] not in selected_ids,
                        "ground_truth_path_count": int(scores["ground_truth_path_count"]),
                        "uncertainty_aggregation": trajectory["uncertainty_aggregation"],
                        **{column: scores[column] for column in SCORE_COLUMNS},
                    }
                )
            _write_csv(round_dir / "model-scores.csv", score_fields, score_rows)

    score_output = output_dir / "model-scores.csv"
    if not score_output.exists():
        _write_csv(score_output, score_fields, [])
    if prediction_audit_rows:
        _write_csv(
            output_dir / "full-space-predictions-all-rounds.csv",
            list(prediction_audit_rows[0]),
            prediction_audit_rows,
        )
    _write_json(
        output_dir / "completed.json",
        {
            "trajectory_id": trajectory["trajectory_id"],
            "rounds_completed": len(curve_rows),
            "selected_pairs": len(selected_rows),
            "ground_truth_rows": len(ground_truth),
            "final_cumulative_sampling_cost_core_hours": cumulative_cost,
            "status": "completed",
        },
    )
    return {
        "trajectory_id": trajectory["trajectory_id"],
        "curve": str(output_dir / "curve.csv"),
        "rounds": len(curve_rows),
        "selected_pairs": len(selected_rows),
        "final_cost_core_hours": cumulative_cost,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_trajectory(args.request.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
