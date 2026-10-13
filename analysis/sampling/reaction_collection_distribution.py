#!/usr/bin/env python3
"""Summarize explicitly selected reaction collections against ground truth.

The database exports use different atom-map numbering and, for some
heteroatom oxides, different but equivalent charge/bond representations.  A
reaction-path key therefore removes atom maps, canonicalizes every molecule
with RDKit, and normalizes ``X(+)-O(-)`` single bonds to ``X=O`` before
canonicalization.  Duplicate rows with the same key are retained in the raw
counts and averaged only for the ground-truth comparison.

These text keys describe path-level distribution statistics. Native dataset
adaptation lives in tricycle_reaction_db.substrate_adaptation; distribution
keys must not be used as substrate identity or completeness evidence.

The resource table is the authoritative source for attempted pair coverage
and compute cost.  ``ts_compute_seconds_sum`` is treated as seconds and is
converted to core-hours by dividing by 3600; missing or invalid seconds are
zero.  For autode, this resource table contains all mapped-reaction
calculation artifacts except single-point artifacts, including reactant and
product optimizations and transition-state/NEB calculations.  The reaction
collection is the authoritative source for a successful pair/path label.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

from tricycle_core import timestamped_run_directory


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return [dict(row) for row in reader]


def _number(value: object, *, default: float = 0.0) -> float:
    if value is None or not str(value).strip():
        return default
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _pair(row: Mapping[str, str]) -> tuple[str, str]:
    ene_id = str(row.get("ene_id", "")).strip()
    diene_id = str(row.get("diene_id", "")).strip()
    if not ene_id or not diene_id:
        raise ValueError(f"row has no complete substrate pair: {dict(row)!r}")
    return ene_id, diene_id


@cache
def _normalized_molecule(smiles: str) -> str:
    """Distribution text key; not a substrate identity proof."""
    try:
        from rdkit import Chem
        from rdkit.Chem.MolStandardize import rdMolStandardize
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError("RDKit is required for reaction-path normalization") from exc

    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        raise ValueError(f"cannot parse molecule SMILES: {smiles!r}")
    for atom in molecule.GetAtoms():
        atom.SetAtomMapNum(0)

    # The database contains both neutral X=O and charge-separated X(+)-O(-)
    # spellings.  Normalize the latter before RDKit's standard cleanup so the
    # two representations produce the same path key.
    editable = Chem.RWMol(molecule)
    for bond in editable.GetBonds():
        left = bond.GetBeginAtom()
        right = bond.GetEndAtom()
        charge_separated_oxide = (
            bond.GetBondType() == Chem.BondType.SINGLE
            and (
                (
                    left.GetSymbol() == "O"
                    and left.GetFormalCharge() == -1
                    and right.GetFormalCharge() == 1
                )
                or (
                    right.GetSymbol() == "O"
                    and right.GetFormalCharge() == -1
                    and left.GetFormalCharge() == 1
                )
            )
        )
        if charge_separated_oxide:
            left.SetFormalCharge(0)
            right.SetFormalCharge(0)
            bond.SetBondType(Chem.BondType.DOUBLE)

    normalized = editable.GetMol()
    Chem.SanitizeMol(normalized)
    normalized = rdMolStandardize.Cleanup(normalized)
    return Chem.MolToSmiles(normalized, canonical=True, isomericSmiles=True)


@cache
def _reaction_key(smiles: str) -> tuple[tuple[str, ...], str]:
    parts = smiles.split(">>")
    if len(parts) != 2:
        raise ValueError(f"invalid reaction SMILES: {smiles!r}")
    reactants, product = parts
    normalized_reactants = tuple(
        sorted(_normalized_molecule(molecule) for molecule in reactants.split(".") if molecule)
    )
    if not normalized_reactants or not product:
        raise ValueError(f"reaction has an empty side: {smiles!r}")
    return normalized_reactants, _normalized_molecule(product)


def _path_key(row: Mapping[str, str]) -> tuple[str, str, tuple[tuple[str, ...], str]]:
    ene_id, diene_id = _pair(row)
    return ene_id, diene_id, _reaction_key(row["mapped_reaction_smiles"])


def _aggregate_paths(
    rows: Iterable[Mapping[str, str]],
) -> dict[tuple[str, str, tuple[tuple[str, ...], str]], list[float]]:
    values: defaultdict[tuple[str, str, tuple[tuple[str, ...], str]], list[float]] = defaultdict(list)
    for row in rows:
        value = _number(row.get("activation_free_energy_kcal_mol"), default=math.nan)
        if not math.isfinite(value):
            raise ValueError(f"reaction row has invalid activation energy: {dict(row)!r}")
        values[_path_key(row)].append(value)
    return dict(values)


def _resource_aggregation(rows: Iterable[Mapping[str, str]]) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    artifact_seconds: dict[str, float] = {}
    artifact_pair_counts: defaultdict[str, int] = defaultdict(int)
    missing_seconds = 0
    hour_field_mismatches = 0
    total_seconds = 0.0
    total_files = 0
    for row in rows:
        pair = _pair(row)
        if pair in by_pair:
            raise ValueError(f"duplicate resource pair: {pair}")
        raw_seconds = str(row.get("ts_compute_seconds_sum", "")).strip()
        seconds = _number(raw_seconds, default=0.0)
        if not raw_seconds or not math.isfinite(_number(raw_seconds, default=math.nan)):
            missing_seconds += 1
        explicit_hours = _number(row.get("ts_compute_hours_sum"), default=math.nan)
        if math.isfinite(explicit_hours) and abs(explicit_hours - seconds / 3600.0) > 1e-6:
            hour_field_mismatches += 1
        ts_files = int(_number(row.get("ts_file_count"), default=0.0))
        raw_ts_files = str(row.get("ts_files", "")).strip()
        if raw_ts_files:
            try:
                parsed_ts_files = json.loads(raw_ts_files)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid ts_files JSON for pair {pair}: {exc}") from exc
            if not isinstance(parsed_ts_files, list):
                raise ValueError(f"ts_files must be a JSON list for pair {pair}")
            pair_artifact_ids: set[str] = set()
            for item in parsed_ts_files:
                if not isinstance(item, Mapping):
                    raise ValueError(f"ts_files item must be an object for pair {pair}")
                artifact_id = str(item.get("artifact_id", "")).strip()
                if not artifact_id or artifact_id in pair_artifact_ids:
                    continue
                pair_artifact_ids.add(artifact_id)
                runtime_seconds = _number(item.get("running_time_seconds"), default=0.0)
                previous_runtime = artifact_seconds.get(artifact_id)
                if previous_runtime is not None and abs(previous_runtime - runtime_seconds) > 1e-6:
                    raise ValueError(
                        f"artifact {artifact_id} has inconsistent running times: "
                        f"{previous_runtime} vs {runtime_seconds}"
                    )
                artifact_seconds[artifact_id] = runtime_seconds
            for artifact_id in pair_artifact_ids:
                artifact_pair_counts[artifact_id] += 1
        total_seconds += seconds
        total_files += ts_files
        by_pair[pair] = {
            "requested_reaction_count": int(
                _number(row.get("requested_reaction_count"), default=0.0)
            ),
            "ts_file_count": ts_files,
            "ts_success_file_count": int(
                _number(row.get("ts_success_file_count"), default=0.0)
            ),
            "ts_failed_file_count": int(
                _number(row.get("ts_failed_file_count"), default=0.0)
            ),
            "ts_status_not_reported_count": int(
                _number(row.get("ts_status_not_reported_count"), default=0.0)
            ),
            "ts_unknown_status_count": int(
                _number(row.get("ts_unknown_status_count"), default=0.0)
            ),
            "ts_compute_seconds_sum": seconds,
            "ts_compute_core_hours": seconds / 3600.0,
        }
    return by_pair, {
        "resource_pair_count": len(by_pair),
        "attempted_pair_count": sum(
            1 for value in by_pair.values() if value["ts_file_count"] > 0
        ),
        "resource_success_status_pair_count": sum(
            1 for value in by_pair.values() if value["ts_success_file_count"] > 0
        ),
        "resource_failed_status_pair_count": sum(
            1 for value in by_pair.values() if value["ts_failed_file_count"] > 0
        ),
        "resource_status_not_reported_pair_count": sum(
            1 for value in by_pair.values() if value["ts_status_not_reported_count"] > 0
        ),
        "total_ts_file_count": total_files,
        "total_ts_compute_seconds": total_seconds,
        "total_ts_compute_core_hours": total_seconds / 3600.0,
        "unique_ts_artifact_count": len(artifact_seconds),
        "shared_ts_artifact_count": sum(
            1 for count in artifact_pair_counts.values() if count > 1
        ),
        "total_unique_ts_compute_seconds": sum(artifact_seconds.values()),
        "total_unique_ts_compute_core_hours": sum(artifact_seconds.values()) / 3600.0,
        "missing_ts_compute_seconds_pair_count": missing_seconds,
        "hour_field_mismatch_pair_count": hour_field_mismatches,
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _key_text(key: tuple[tuple[str, ...], str]) -> str:
    return json.dumps(
        {"reactants": list(key[0]), "product": key[1]},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _key_digest(key: tuple[tuple[str, ...], str]) -> str:
    return hashlib.sha256(_key_text(key).encode("utf-8")).hexdigest()


def _write_csv(path: Path, fieldnames: list[str], rows: Iterable[Mapping[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _format(value: object) -> str:
    if isinstance(value, float):
        return format(value, ".12g")
    return str(value)


def _summary_markdown_table(summary_rows: Iterable[Mapping[str, str]]) -> str:
    lines = [
        "| 项目 | 尝试底物组合 | 资源关联核时 (core-hours) | 全局去重核时 (core-hours) | 成功底物组合 | 成功反应路径 | 基准 MAE (kcal/mol) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary_rows:
        lines.append(
            "| {project} | {attempted}/{expected} ({attempted_pct}) | {hours} | "
            "{unique_hours} | "
            "{successful_pairs}/{expected} ({successful_pct}) | "
            "{matched_paths}/{gt_paths} ({path_pct}) | {mae} |".format(
                project=row["project"],
                attempted=row["attempted_substrate_pair_count"],
                expected=row["expected_substrate_pair_count"],
                attempted_pct=f"{float(row['attempted_substrate_pair_coverage']) * 100:.4f}%",
                hours=f"{float(row['total_ts_compute_core_hours']):,.2f}",
                unique_hours=f"{float(row['total_unique_ts_compute_core_hours']):,.2f}",
                successful_pairs=row["successful_label_substrate_pair_count"],
                successful_pct=(
                    f"{float(row['successful_label_substrate_pair_coverage']) * 100:.4f}%"
                ),
                matched_paths=row["matched_ground_truth_reaction_path_count"],
                gt_paths=row["ground_truth_unique_reaction_path_count"],
                path_pct=f"{float(row['successful_reaction_path_coverage']) * 100:.4f}%",
                mae=f"{float(row['baseline_mae_kcal_mol']):.4f}",
            )
        )
    return "\n".join(lines)


def analyze(
    *,
    ground_truth_dir: Path,
    projects: Mapping[str, Path],
    output_dir: Path,
    expected_pair_count: int,
) -> dict[str, Any]:
    if expected_pair_count < 1:
        raise ValueError("expected_pair_count must be positive")
    if not projects:
        raise ValueError("at least one project is required")
    ground_truth_rows = _read_csv(ground_truth_dir / "reaction-collection.csv")
    ground_truth_resource_rows = _read_csv(ground_truth_dir / "ts-time-aggregation.csv")
    ground_truth_paths = _aggregate_paths(ground_truth_rows)
    ground_truth_pairs = {_pair(row) for row in ground_truth_rows}
    if len(ground_truth_pairs) != expected_pair_count:
        raise ValueError(
            f"ground truth has {len(ground_truth_pairs)} substrate pairs, "
            f"expected {expected_pair_count}"
        )

    output_dir.mkdir(parents=True, exist_ok=False)

    summary_rows: list[dict[str, str]] = []
    pair_rows: list[dict[str, object]] = []
    baseline_rows: list[dict[str, object]] = []
    audit: dict[str, Any] = {
        "ground_truth": {
            "reaction_collection_row_count": len(ground_truth_rows),
            "unique_reaction_path_count": len(ground_truth_paths),
            "substrate_pair_count": len(ground_truth_pairs),
        },
        "projects": {},
        "path_key": (
            "atom maps removed; molecules RDKit-canonicalized; reactant order sorted; "
            "charge-separated X(+)-O(-) single bonds normalized to X=O"
        ),
        "cost": (
            "resource-associated sum of ts_compute_seconds_sum, with "
            "missing/invalid seconds as 0, divided by 3600; unique artifact "
            "costs are also reported after de-duplication by artifact id"
        ),
    }

    ground_truth_resource_by_pair, ground_truth_resource_summary = _resource_aggregation(
        ground_truth_resource_rows
    )
    ground_truth_requested_pairs = set(ground_truth_resource_by_pair)
    ground_truth_attempted_pairs = {
        pair
        for pair, value in ground_truth_resource_by_pair.items()
        if value["ts_file_count"] > 0
    }
    ground_truth_summary = {
        "project": "ground-truth",
        "expected_substrate_pair_count": expected_pair_count,
        "requested_substrate_pair_count": len(ground_truth_requested_pairs),
        "requested_substrate_pair_coverage": (
            len(ground_truth_requested_pairs) / expected_pair_count
        ),
        "attempted_substrate_pair_count": len(ground_truth_attempted_pairs),
        "attempted_substrate_pair_coverage": (
            len(ground_truth_attempted_pairs) / expected_pair_count
        ),
        "total_ts_compute_seconds": ground_truth_resource_summary[
            "total_ts_compute_seconds"
        ],
        "total_ts_compute_core_hours": ground_truth_resource_summary[
            "total_ts_compute_core_hours"
        ],
        "unique_ts_artifact_count": ground_truth_resource_summary[
            "unique_ts_artifact_count"
        ],
        "shared_ts_artifact_count": ground_truth_resource_summary[
            "shared_ts_artifact_count"
        ],
        "total_unique_ts_compute_seconds": ground_truth_resource_summary[
            "total_unique_ts_compute_seconds"
        ],
        "total_unique_ts_compute_core_hours": ground_truth_resource_summary[
            "total_unique_ts_compute_core_hours"
        ],
        "total_ts_file_count": ground_truth_resource_summary["total_ts_file_count"],
        "successful_label_substrate_pair_count": len(ground_truth_pairs),
        "successful_label_substrate_pair_coverage": (
            len(ground_truth_pairs) / expected_pair_count
        ),
        "successful_reaction_path_row_count": len(ground_truth_rows),
        "successful_unique_reaction_path_count": len(ground_truth_paths),
        "ground_truth_reaction_collection_row_count": len(ground_truth_rows),
        "ground_truth_unique_reaction_path_count": len(ground_truth_paths),
        "matched_ground_truth_reaction_collection_row_count": len(ground_truth_rows),
        "matched_ground_truth_reaction_collection_row_coverage": 1.0,
        "matched_ground_truth_reaction_path_count": len(ground_truth_paths),
        "successful_reaction_path_coverage": 1.0,
        "project_only_unique_reaction_path_count": 0,
        "baseline_mae_kcal_mol": 0.0,
        "baseline_rmse_kcal_mol": 0.0,
        "baseline_bias_kcal_mol": 0.0,
        "baseline_max_absolute_error_kcal_mol": 0.0,
        **ground_truth_resource_summary,
    }
    summary_rows.append({key: _format(value) for key, value in ground_truth_summary.items()})
    audit["ground_truth"].update(
        {
            "resource": ground_truth_resource_summary,
            "attempted_substrate_pair_count": len(ground_truth_attempted_pairs),
            "total_ts_compute_core_hours": ground_truth_resource_summary[
                "total_ts_compute_core_hours"
            ],
            "total_unique_ts_compute_core_hours": ground_truth_resource_summary[
                "total_unique_ts_compute_core_hours"
            ],
        }
    )
    audit["projects"]["ground-truth"] = ground_truth_summary

    for project, project_dir in projects.items():
        collection_rows = _read_csv(project_dir / "reaction-collection.csv")
        resource_rows = _read_csv(project_dir / "ts-time-aggregation.csv")
        project_paths = _aggregate_paths(collection_rows)
        resource_by_pair, resource_summary = _resource_aggregation(resource_rows)
        project_pairs = {_pair(row) for row in collection_rows}
        requested_pairs = set(resource_by_pair)
        attempted_pairs = {
            pair for pair, value in resource_by_pair.items() if value["ts_file_count"] > 0
        }
        successful_path_pairs = project_pairs
        matched_keys = sorted(set(project_paths) & set(ground_truth_paths))

        errors: list[float] = []
        signed_errors: list[float] = []
        for ene_id, diene_id, key in matched_keys:
            gt_mean = _mean(ground_truth_paths[(ene_id, diene_id, key)])
            project_mean = _mean(project_paths[(ene_id, diene_id, key)])
            signed_error = project_mean - gt_mean
            absolute_error = abs(signed_error)
            errors.append(absolute_error)
            signed_errors.append(signed_error)
            baseline_rows.append(
                {
                    "project": project,
                    "ene_id": ene_id,
                    "diene_id": diene_id,
                    "reaction_key_sha256": _key_digest(key),
                    "normalized_reactants": ".".join(key[0]),
                    "normalized_product": key[1],
                    "ground_truth_row_count": len(ground_truth_paths[(ene_id, diene_id, key)]),
                    "project_row_count": len(project_paths[(ene_id, diene_id, key)]),
                    "ground_truth_activation_free_energy_kcal_mol": _format(gt_mean),
                    "project_activation_free_energy_kcal_mol": _format(project_mean),
                    "signed_error_kcal_mol": _format(signed_error),
                    "absolute_error_kcal_mol": _format(absolute_error),
                }
            )

        matched_ground_truth_row_count = sum(
            len(ground_truth_paths[(ene_id, diene_id, key)])
            for ene_id, diene_id, key in matched_keys
        )
        valid_path_pair_count = len(successful_path_pairs)
        summary = {
            "project": project,
            "expected_substrate_pair_count": expected_pair_count,
            "requested_substrate_pair_count": len(requested_pairs),
            "requested_substrate_pair_coverage": len(requested_pairs) / expected_pair_count,
            "attempted_substrate_pair_count": len(attempted_pairs),
            "attempted_substrate_pair_coverage": len(attempted_pairs) / expected_pair_count,
            "total_ts_compute_seconds": resource_summary["total_ts_compute_seconds"],
            "total_ts_compute_core_hours": resource_summary["total_ts_compute_core_hours"],
            "unique_ts_artifact_count": resource_summary["unique_ts_artifact_count"],
            "shared_ts_artifact_count": resource_summary["shared_ts_artifact_count"],
            "total_unique_ts_compute_seconds": resource_summary["total_unique_ts_compute_seconds"],
            "total_unique_ts_compute_core_hours": resource_summary[
                "total_unique_ts_compute_core_hours"
            ],
            "total_ts_file_count": resource_summary["total_ts_file_count"],
            "successful_label_substrate_pair_count": valid_path_pair_count,
            "successful_label_substrate_pair_coverage": valid_path_pair_count / expected_pair_count,
            "successful_reaction_path_row_count": len(collection_rows),
            "successful_unique_reaction_path_count": len(project_paths),
            "ground_truth_reaction_collection_row_count": len(ground_truth_rows),
            "ground_truth_unique_reaction_path_count": len(ground_truth_paths),
            "matched_ground_truth_reaction_collection_row_count": matched_ground_truth_row_count,
            "matched_ground_truth_reaction_collection_row_coverage": (
                matched_ground_truth_row_count / len(ground_truth_rows)
            ),
            "matched_ground_truth_reaction_path_count": len(matched_keys),
            "successful_reaction_path_coverage": len(matched_keys) / len(ground_truth_paths),
            "project_only_unique_reaction_path_count": len(set(project_paths) - set(ground_truth_paths)),
            "baseline_mae_kcal_mol": _mean(errors) if errors else math.nan,
            "baseline_rmse_kcal_mol": (
                math.sqrt(sum(error * error for error in errors) / len(errors))
                if errors
                else math.nan
            ),
            "baseline_bias_kcal_mol": _mean(signed_errors) if signed_errors else math.nan,
            "baseline_max_absolute_error_kcal_mol": max(errors) if errors else math.nan,
            **resource_summary,
        }
        summary_rows.append({key: _format(value) for key, value in summary.items()})

        collection_count_by_pair: defaultdict[tuple[str, str], int] = defaultdict(int)
        collection_keys_by_pair: defaultdict[tuple[str, str], set[Any]] = defaultdict(set)
        for row in collection_rows:
            path_key = _path_key(row)
            substrate_pair = (path_key[0], path_key[1])
            collection_count_by_pair[substrate_pair] += 1
            collection_keys_by_pair[substrate_pair].add(path_key[2])
        for resource_pair, resource in sorted(resource_by_pair.items()):
            pair_rows.append(
                {
                    "project": project,
                    "ene_id": resource_pair[0],
                    "diene_id": resource_pair[1],
                    "requested_reaction_count": resource["requested_reaction_count"],
                    "ts_file_count": resource["ts_file_count"],
                    "ts_success_file_count": resource["ts_success_file_count"],
                    "ts_failed_file_count": resource["ts_failed_file_count"],
                    "ts_status_not_reported_count": resource["ts_status_not_reported_count"],
                    "ts_unknown_status_count": resource["ts_unknown_status_count"],
                    "ts_compute_seconds_sum": _format(resource["ts_compute_seconds_sum"]),
                    "ts_compute_core_hours": _format(resource["ts_compute_core_hours"]),
                    "attempted": str(resource["ts_file_count"] > 0).lower(),
                    "successful_label": str(resource_pair in successful_path_pairs).lower(),
                    "successful_reaction_path_row_count": collection_count_by_pair[resource_pair],
                    "successful_unique_reaction_path_count": len(collection_keys_by_pair[resource_pair]),
                }
            )
        audit["projects"][project] = summary

    summary_fields = list(summary_rows[0]) if summary_rows else []
    _write_csv(output_dir / "distribution-summary.csv", summary_fields, summary_rows)
    _write_csv(
        output_dir / "pair-distribution.csv",
        [
            "project",
            "ene_id",
            "diene_id",
            "requested_reaction_count",
            "ts_file_count",
            "ts_success_file_count",
            "ts_failed_file_count",
            "ts_status_not_reported_count",
            "ts_unknown_status_count",
            "ts_compute_seconds_sum",
            "ts_compute_core_hours",
            "attempted",
            "successful_label",
            "successful_reaction_path_row_count",
            "successful_unique_reaction_path_count",
        ],
        pair_rows,
    )
    _write_csv(
        output_dir / "path-baseline.csv",
        [
            "project",
            "ene_id",
            "diene_id",
            "reaction_key_sha256",
            "normalized_reactants",
            "normalized_product",
            "ground_truth_row_count",
            "project_row_count",
            "ground_truth_activation_free_energy_kcal_mol",
            "project_activation_free_energy_kcal_mol",
            "signed_error_kcal_mol",
            "absolute_error_kcal_mol",
        ],
        baseline_rows,
    )
    (output_dir / "distribution-report.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    summary_table = _summary_markdown_table(summary_rows)
    (output_dir / "README.md").write_text(
        f"""# Reaction collection distribution

## Summary

{summary_table}

`distribution-summary.csv` contains one row per analyzed collection plus a
`ground-truth` reference row. The ground-truth coverage and baseline MAE are
reported against itself (therefore complete coverage and zero error), while
its resource fields come from the ground-truth resource export.

The coverage fields distinguish the requested {expected_pair_count}-pair space
from pairs that actually have at least one counted calculation artifact.

- `attempted_substrate_pair_coverage`: pairs with `ts_file_count > 0` / {expected_pair_count};
  for autode, `ts_file_count` means all associated non-single-point calculation
  artifacts, not only transition-state files.
- `total_ts_compute_core_hours`: resource-associated sum of
  `ts_compute_seconds_sum` / 3600, including unsuccessful and status-unknown
  artifacts. This is the cost used by the active-learning curves: a shared
  calculation is charged to every substrate pair that receives it.
- `total_unique_ts_compute_core_hours`: sum of each distinct artifact's
  `running_time_seconds` / 3600, counted once per project. This is the
  project-wide de-duplicated compute total; `shared_ts_artifact_count`
  reports artifacts referenced by more than one substrate pair.
- `successful_label_substrate_pair_coverage`: pairs with at least one valid
  activation-energy row / {expected_pair_count}.
- `successful_reaction_path_coverage`: unique normalized reaction paths shared
  with ground truth / unique normalized ground-truth paths.
- `matched_ground_truth_reaction_collection_row_coverage`: the same match
  projected back to the raw ground-truth row denominator ({len(ground_truth_rows)} rows).
- `baseline_mae_kcal_mol`: MAE of per-path mean activation energy on the shared
  normalized paths. Duplicate database rows are retained in raw counts and
  averaged only for this comparison.

Path normalization removes atom maps, canonicalizes molecules with RDKit,
sorts reactants, and treats neutral `X=O` and charge-separated `X(+)-O(-)`
spellings as equivalent. `pair-distribution.csv` gives the complete {expected_pair_count}-pair
audit, while `path-baseline.csv` contains every path used in each project's
MAE.
""",
        encoding="utf-8",
    )
    return audit


def _parse_project(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("project must be NAME=EXPORT_DIR")
    name, directory = value.split("=", 1)
    if not name.strip() or not directory.strip():
        raise argparse.ArgumentTypeError("project must be NAME=EXPORT_DIR")
    return name.strip(), Path(directory.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--expected-pairs", type=int, required=True)
    parser.add_argument(
        "--project",
        action="append",
        required=True,
        type=_parse_project,
        metavar="NAME=EXPORT_DIR",
        help="explicit project export; may be repeated",
    )
    args = parser.parse_args()
    output_dir = args.output_dir or timestamped_run_directory(
        Path("runs"), "reaction-collection-distribution"
    )
    projects = dict(args.project)
    if len(projects) != len(args.project):
        parser.error("project names must be unique")
    analyze(
        ground_truth_dir=args.ground_truth,
        projects=projects,
        output_dir=output_dir,
        expected_pair_count=args.expected_pairs,
    )
    print(f"wrote distribution report to {output_dir}")


if __name__ == "__main__":
    main()
