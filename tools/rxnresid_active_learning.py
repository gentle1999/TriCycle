#!/usr/bin/env python3
"""Prepare and collect the RxnResid substrate-pair active-learning experiment.

The experiment is intentionally a small orchestration layer around the pinned
RxnResid repository.  It normalizes the database exports locally, stages one
worker bundle per project, and emits one Bohr request per complete trajectory.
The worker itself is in ``rxnresid_active_learning_runner.py``; it performs all
rounds in one Bohr task.

No database or Bohr network call is made by ``validate`` or ``prepare``.
Submission is a separate explicit command and is implemented by the normal
TriCycle Bohr adapter through the application CLI.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tomllib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from tricycle_core import timestamped_run_directory

REPO_ROOT = Path(__file__).resolve().parents[1]
RXNRESID_REPOSITORY = "https://github.com/gentle1999/RxnResid"
RXNRESID_REVISION = "9be807a5b2fa0726aacb7d40f57fe973d6dcadc3"
RXNRESID_IMAGE = "registry.dp.tech/dptech/dp/native/prod-23278/dl:260828"
DEFAULT_MACHINE_TYPE = "1 * NVIDIA T4_16g"
DEFAULT_PROJECT_ID = "4657140"
DEFAULT_CONFIG = REPO_ROOT / "configs" / "rxnresid-active-learning.toml"
STRATEGIES = (
    "random",
    "predictive-uncertainty",
    "epistemic-uncertainty",
    "aleatoric-uncertainty",
)
SUPPORTED_STRATEGIES = STRATEGIES + (
    "pair-core-set",
    "density-weighted-uncertainty",
    "uncertainty-diversity",
)
DEFAULT_DIVERSITY_WEIGHT = 0.5
PAIR_FINGERPRINT_BITS = 256
PROJECTS = ("autode", "dpa-fined-rits-da", "rits-zero-shot-da")
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


@dataclass(frozen=True, slots=True)
class ProjectSpec:
    name: str
    collection: Path
    resources: Path


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    config_path: Path
    repo_root: Path
    output_root: Path
    ground_truth_collection: Path
    ground_truth_resources: Path | None
    projects: tuple[ProjectSpec, ...]
    source_directory: Path | None
    source_repository: str
    source_revision: str
    bohr_project_id: str
    bohr_machine_type: str
    bohr_image: str
    max_run_time: int | None
    strategies: tuple[str, ...]
    seeds: tuple[int, ...]
    pair_batch_size: int
    round_count: int
    expected_pair_count: int
    uncertainty_aggregation: str
    cost_column: str
    cost_unit: str
    training_epochs: int
    training_batch_size_paths: int
    training_device: str
    training_mixed_precision: str
    persist_predictions: bool
    diversity_weight: float = DEFAULT_DIVERSITY_WEIGHT
    fingerprint_bits: int = PAIR_FINGERPRINT_BITS
    fixed_seed: int = 42


@dataclass(frozen=True, slots=True)
class NormalizedCollection:
    name: str
    source_path: Path
    native_rows: tuple[dict[str, str], ...]
    rows_by_pair: Mapping[tuple[str, str], tuple[dict[str, str], ...]]
    pair_metadata: Mapping[tuple[str, str], Mapping[str, str]]

    @property
    def pairs(self) -> frozenset[tuple[str, str]]:
        return frozenset(self.rows_by_pair)


def _id_sort_key(value: str) -> tuple[int, int | str]:
    try:
        return (0, int(value))
    except ValueError:
        return (1, value)


def pair_sort_key(pair: tuple[str, str]) -> tuple[tuple[int, int | str], tuple[int, int | str]]:
    return (_id_sort_key(pair[0]), _id_sort_key(pair[1]))


def pair_id(ene_id: str, diene_id: str) -> str:
    return f"{ene_id}+{diene_id}"


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"CSV file not found: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t" if path.suffix == ".tsv" else ",")
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return [dict(row) for row in reader]


def _required(row: Mapping[str, str], column: str, *, path: Path, row_index: int) -> str:
    value = row.get(column, "").strip()
    if not value:
        raise ValueError(f"{path}: row {row_index} has empty required column {column!r}")
    return value


def _finite_float(value: str, *, path: Path, row_index: int, column: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: row {row_index} has non-numeric {column!r}: {value!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{path}: row {row_index} has non-finite {column!r}")
    return parsed


def parse_materialized_cost(value: object) -> tuple[float, bool]:
    """Parse the already-materialized core-hour value without conversion.

    The export contains calculation times in seconds and a derived hours
    column.  This helper parses whichever explicitly configured cost column is
    supplied; unit conversion belongs to the export/configuration boundary,
    not to this generic parser.  Missing or blank values become zero and are
    marked for the audit table.
    """

    if value is None or not str(value).strip():
        return 0.0, True
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0, True
    if not math.isfinite(parsed):
        return 0.0, True
    if parsed < 0.0:
        raise ValueError(f"sampling cost must be non-negative, got {parsed}")
    return parsed, False


def normalize_collection(path: Path, name: str, *, max_routes: int = 8) -> NormalizedCollection:
    """Convert one database collection to the native RxnResid path-wise CSV."""

    source_rows = _read_csv(path)
    required_columns = (
        "mapped_reaction_smiles",
        "activation_free_energy_kcal_mol",
        "ene_id",
        "diene_id",
        "ene_smiles",
        "diene_smiles",
    )
    if source_rows:
        missing = [column for column in required_columns if column not in source_rows[0]]
        if missing:
            raise ValueError(f"{path}: missing columns: {', '.join(missing)}")
    if not source_rows:
        raise ValueError(f"collection is empty: {path}")

    prepared: list[dict[str, object]] = []
    for row_index, row in enumerate(source_rows):
        reaction = _required(row, "mapped_reaction_smiles", path=path, row_index=row_index)
        target = _finite_float(
            _required(row, "activation_free_energy_kcal_mol", path=path, row_index=row_index),
            path=path,
            row_index=row_index,
            column="activation_free_energy_kcal_mol",
        )
        ene_id = _required(row, "ene_id", path=path, row_index=row_index)
        diene_id = _required(row, "diene_id", path=path, row_index=row_index)
        ene_smiles = _required(row, "ene_smiles", path=path, row_index=row_index)
        diene_smiles = _required(row, "diene_smiles", path=path, row_index=row_index)
        mapped_id = row.get("mapped_reaction_id", "").strip() or f"{name}-source-{row_index:08d}"
        prepared.append(
            {
                "source_row_index": row_index,
                "mapped_reaction_id": mapped_id,
                "mapped_reaction_smiles": reaction,
                "activation": target,
                "ene_id": ene_id,
                "diene_id": diene_id,
                "ene_smiles": ene_smiles,
                "diene_smiles": diene_smiles,
            }
        )

    prepared.sort(
        key=lambda row: (
            pair_sort_key((str(row["ene_id"]), str(row["diene_id"]))),
            str(row["mapped_reaction_id"]),
            str(row["mapped_reaction_smiles"]),
            int(row["source_row_index"]),
        )
    )
    route_counts: defaultdict[tuple[str, str], int] = defaultdict(int)
    rows_by_pair_mutable: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    pair_metadata: dict[tuple[str, str], Mapping[str, str]] = {}
    native_rows: list[dict[str, str]] = []
    for normalized_index, row in enumerate(prepared):
        ene_id = str(row["ene_id"])
        diene_id = str(row["diene_id"])
        pair = (ene_id, diene_id)
        route_id = route_counts[pair]
        route_counts[pair] += 1
        if route_id >= max_routes:
            raise ValueError(
                f"{path}: pair {pair_id(*pair)} has more than {max_routes} routes; "
                "RxnResid route_id_count would be insufficient"
            )
        native = {
            "path_id": f"{name}-path-{normalized_index:08d}",
            "source_mapped_reaction_id": str(row["mapped_reaction_id"]),
            "source_row_index": str(row["source_row_index"]),
            "rxn_smiles": str(row["mapped_reaction_smiles"]),
            "G_T_activate": format(float(row["activation"]), ".12g"),
            "ene_id": ene_id,
            "diene_id": diene_id,
            "prod_id": str(route_id),
        }
        native_rows.append(native)
        rows_by_pair_mutable[pair].append(native)
        pair_metadata.setdefault(
            pair,
            {
                "ene_id": ene_id,
                "diene_id": diene_id,
                "ene_smiles": str(row["ene_smiles"]),
                "diene_smiles": str(row["diene_smiles"]),
            },
        )

    return NormalizedCollection(
        name=name,
        source_path=path,
        native_rows=tuple(native_rows),
        rows_by_pair={pair: tuple(rows) for pair, rows in rows_by_pair_mutable.items()},
        pair_metadata=pair_metadata,
    )


def _morgan_bitset(smiles: str, *, cache: dict[str, int], bits: int) -> int:
    """Create a compact Morgan bitset for a substrate without storing RDKit objects."""

    if smiles in cache:
        return cache[smiles]
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
    except ImportError as exc:
        raise ValueError(
            "RDKit is required to prepare pair-core-set and density-aware sampling features"
        ) from exc
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        raise ValueError(f"cannot parse substrate SMILES for sampling features: {smiles!r}")
    generator = AllChem.GetMorganGenerator(radius=2, fpSize=bits)
    fingerprint = generator.GetFingerprint(molecule)
    bitset = sum(
        1 << index for index in range(fingerprint.GetNumBits()) if fingerprint.GetBit(index)
    )
    cache[smiles] = bitset
    return bitset


def _pair_tanimoto(left: int, right: int) -> float:
    union = (left | right).bit_count()
    return (left & right).bit_count() / union if union else 1.0


def _pair_sampling_features(
    pairs: Sequence[tuple[str, str]],
    metadata: Mapping[tuple[str, str], Mapping[str, str]],
    *,
    bits: int,
) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], float]]:
    substrate_cache: dict[str, int] = {}
    pair_embeddings: dict[tuple[str, str], int] = {}
    for pair in pairs:
        pair_metadata = metadata[pair]
        ene = _morgan_bitset(str(pair_metadata["ene_smiles"]), cache=substrate_cache, bits=bits)
        diene = _morgan_bitset(
            str(pair_metadata["diene_smiles"]), cache=substrate_cache, bits=bits
        )
        pair_embeddings[pair] = ene | (diene << bits)

    pair_density: dict[tuple[str, str], float] = {}
    for pair in pairs:
        embedding = pair_embeddings[pair]
        other_embeddings = (pair_embeddings[other] for other in pairs if other != pair)
        similarities = [_pair_tanimoto(embedding, other) for other in other_embeddings]
        pair_density[pair] = sum(similarities) / len(similarities) if similarities else 1.0
    return pair_embeddings, pair_density


def build_pair_pool(
    ground_truth: NormalizedCollection,
    project: NormalizedCollection,
    resources_path: Path,
    *,
    cost_column: str,
    fingerprint_bits: int = PAIR_FINGERPRINT_BITS,
    cost_unit: str = "core_hours",
) -> list[dict[str, str]]:
    """Build the complete GT pair candidate pool, retaining unlabeled pairs."""

    extra_pairs = project.pairs - ground_truth.pairs
    if extra_pairs:
        sample = ", ".join(pair_id(*pair) for pair in sorted(extra_pairs, key=pair_sort_key)[:5])
        raise ValueError(
            f"{project.name}: collection contains pairs absent from ground truth "
            f"({len(extra_pairs)}; examples: {sample})"
        )
    resource_rows = _read_csv(resources_path)
    resource_by_pair: dict[tuple[str, str], dict[str, str]] = {}
    for row_index, row in enumerate(resource_rows):
        ene_id = _required(row, "ene_id", path=resources_path, row_index=row_index)
        diene_id = _required(row, "diene_id", path=resources_path, row_index=row_index)
        key = (ene_id, diene_id)
        if key in resource_by_pair:
            raise ValueError(f"{resources_path}: duplicate resource pair {pair_id(*key)}")
        resource_by_pair[key] = row

    sorted_pairs = sorted(ground_truth.pairs, key=pair_sort_key)
    pair_embeddings, pair_density = _pair_sampling_features(
        sorted_pairs,
        ground_truth.pair_metadata,
        bits=fingerprint_bits,
    )
    pool: list[dict[str, str]] = []
    for order, pair in enumerate(sorted_pairs):
        metadata = ground_truth.pair_metadata[pair]
        resource = resource_by_pair.get(pair, {})
        cost, missing = parse_materialized_cost(resource.get(cost_column))
        label_count = len(project.rows_by_pair.get(pair, ()))
        pool.append(
            {
                "pair_id": pair_id(*pair),
                "pair_order": str(order),
                "ene_id": pair[0],
                "diene_id": pair[1],
                "ene_smiles": str(metadata["ene_smiles"]),
                "diene_smiles": str(metadata["diene_smiles"]),
                "project_label_row_count": str(label_count),
                "has_valid_training_label": "true" if label_count else "false",
                "resource_cost_raw": resource.get(cost_column, ""),
                "materialized_sampling_cost_core_hours": format(cost, ".12g"),
                "cost_was_missing": "true" if missing else "false",
                "cost_source_column": cost_column,
                "cost_unit": cost_unit,
                "pair_embedding": str(pair_embeddings[pair]),
                "pair_density": format(pair_density[pair], ".12g"),
                "pair_fingerprint_bits": str(fingerprint_bits),
            }
        )
    return pool


def _write_csv(
    path: Path,
    fieldnames: list[str],
    rows: list[Mapping[str, object]],
    *,
    delimiter: str = ",",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            delimiter=delimiter,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in fieldnames} for row in rows)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _link_or_copy(source: Path, destination: Path) -> None:
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.hardlink_to(source)
    except OSError:
        shutil.copy2(source, destination)


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise ValueError(f"source directory does not exist: {source}")
    shutil.copytree(
        source,
        destination,
        copy_function=_link_or_copy,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".cache",
            "__pycache__",
            "*.pyc",
            ".pytest_cache",
            ".ruff_cache",
        ),
    )


def _toml_key(key: str) -> str:
    if key and all(character.isalnum() or character in "-_" for character in key):
        return key
    return json.dumps(key, ensure_ascii=False)


def _toml_value(value: object) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("TOML does not support non-finite floats")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        return "{" + ", ".join(
            f"{_toml_key(str(key))} = {_toml_value(item)}" for key, item in value.items()
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ValueError(f"unsupported TOML value: {value!r}")


def _toml_lines(mapping: Mapping[str, object], prefix: tuple[str, ...] = ()) -> list[str]:
    scalar_lines: list[str] = []
    sections: list[tuple[str, Mapping[str, object]]] = []
    for raw_key, value in mapping.items():
        key = str(raw_key)
        if isinstance(value, Mapping):
            sections.append((key, value))
        else:
            scalar_lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    lines = list(scalar_lines)
    for key, section in sections:
        if lines:
            lines.append("")
        section_name = ".".join((*prefix, _toml_key(key)))
        lines.append(f"[{section_name}]")
        lines.extend(_toml_lines(section, (*prefix, _toml_key(key))))
    return lines


def _write_toml(path: Path, mapping: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(_toml_lines(mapping)) + "\n", encoding="utf-8")


def _resolve_path(value: object, *, base: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    path = Path(expanded)
    return (path if path.is_absolute() else base / path).resolve()


def _optional_path(value: object, *, base: Path) -> Path | None:
    if value is None or not str(value).strip():
        return None
    return _resolve_path(value, base=base)


def load_experiment_spec(config_path: Path, *, repo_root: Path | None = None) -> ExperimentSpec:
    config_path = config_path.resolve()
    if not config_path.is_file():
        raise ValueError(f"experiment TOML not found: {config_path}")
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    base = (repo_root or REPO_ROOT).resolve()
    dataset = raw.get("dataset", {})
    rxnresid = raw.get("rxnresid", {})
    bohr = raw.get("bohr", {})
    training = raw.get("training", {})
    if not isinstance(dataset, Mapping) or not isinstance(rxnresid, Mapping):
        raise ValueError("[dataset] and [rxnresid] must be TOML tables")
    if not isinstance(bohr, Mapping) or not isinstance(training, Mapping):
        raise ValueError("[bohr] and [training] must be TOML tables")
    project_mapping = raw.get("projects", {})
    if not isinstance(project_mapping, Mapping):
        raise ValueError("[projects] must be a TOML table")
    projects: list[ProjectSpec] = []
    for name in PROJECTS:
        value = project_mapping.get(name)
        if not isinstance(value, Mapping):
            raise ValueError(f"missing [projects.{name}] configuration")
        projects.append(
            ProjectSpec(
                name=name,
                collection=_resolve_path(value.get("collection", ""), base=base),
                resources=_resolve_path(value.get("resources", ""), base=base),
            )
        )
    strategies = tuple(str(item) for item in raw.get("strategies", STRATEGIES))
    if not strategies:
        raise ValueError("strategies must not be empty")
    unsupported = sorted(set(strategies) - set(SUPPORTED_STRATEGIES))
    if unsupported:
        raise ValueError(
            f"unsupported strategies: {unsupported}; supported strategies are "
            f"{list(SUPPORTED_STRATEGIES)}"
        )
    if len(strategies) != len(set(strategies)):
        raise ValueError("strategies must not contain duplicates")
    seeds = tuple(int(item) for item in raw.get("seeds", (0, 1, 2, 3, 4)))
    if len(seeds) != 5 or len(set(seeds)) != 5:
        raise ValueError("seeds must contain five distinct values")
    fixed_seed = int(raw.get("fixed_seed", 42))
    if fixed_seed < 0:
        raise ValueError("fixed_seed must be non-negative")
    pair_batch_size = int(raw.get("pair_batch_size", 40))
    round_count = int(raw.get("round_count", 40))
    expected_pair_count = int(raw.get("expected_pair_count", 1600))
    if pair_batch_size < 1 or round_count < 1 or pair_batch_size * round_count != expected_pair_count:
        raise ValueError("pair_batch_size * round_count must equal expected_pair_count")
    uncertainty_aggregation = str(raw.get("uncertainty_aggregation", "sum")).strip().lower()
    if uncertainty_aggregation != "sum":
        raise ValueError(
            "uncertainty_aggregation must be 'sum' so pair acquisition follows the path-level MAE"
        )
    source_directory = _optional_path(rxnresid.get("source_directory"), base=base)
    max_run_time_raw = bohr.get("max_run_time")
    max_run_time = int(max_run_time_raw) if max_run_time_raw not in (None, "") else None
    diversity_weight = float(raw.get("diversity_weight", DEFAULT_DIVERSITY_WEIGHT))
    if not math.isfinite(diversity_weight) or not 0.0 <= diversity_weight <= 1.0:
        raise ValueError("diversity_weight must be a finite number in [0, 1]")
    fingerprint_bits = int(raw.get("fingerprint_bits", PAIR_FINGERPRINT_BITS))
    if fingerprint_bits < 1:
        raise ValueError("fingerprint_bits must be positive")
    return ExperimentSpec(
        config_path=config_path,
        repo_root=base,
        output_root=(
            _resolve_path(raw["output_root"], base=base)
            if raw.get("output_root")
            else timestamped_run_directory(base / "runs", "rxnresid-active-learning")
        ),
        ground_truth_collection=_resolve_path(dataset.get("ground_truth_collection", ""), base=base),
        ground_truth_resources=_optional_path(dataset.get("ground_truth_resources"), base=base),
        projects=tuple(projects),
        source_directory=source_directory,
        source_repository=str(rxnresid.get("repository", RXNRESID_REPOSITORY)),
        source_revision=str(rxnresid.get("revision", RXNRESID_REVISION)),
        bohr_project_id=str(bohr.get("project_id", DEFAULT_PROJECT_ID)),
        bohr_machine_type=str(bohr.get("machine_type", DEFAULT_MACHINE_TYPE)),
        bohr_image=str(bohr.get("image", RXNRESID_IMAGE)),
        max_run_time=max_run_time,
        strategies=strategies,
        seeds=seeds,
        pair_batch_size=pair_batch_size,
        round_count=round_count,
        expected_pair_count=expected_pair_count,
        uncertainty_aggregation=uncertainty_aggregation,
        cost_column=str(raw.get("cost_column", "ts_compute_hours_sum")),
        cost_unit=str(raw.get("cost_unit", "core_hours")),
        training_epochs=int(training.get("epochs", 300)),
        training_batch_size_paths=int(training.get("batch_size_paths", 512)),
        training_device=str(training.get("device", "auto")),
        training_mixed_precision=str(training.get("mixed_precision", "no")),
        persist_predictions=bool(raw.get("persist_round_predictions", True)),
        diversity_weight=diversity_weight,
        fingerprint_bits=fingerprint_bits,
        fixed_seed=fixed_seed,
    )


def validate_inputs(spec: ExperimentSpec) -> dict[str, Any]:
    ground_truth = normalize_collection(spec.ground_truth_collection, "ground-truth")
    if len(ground_truth.pairs) != spec.expected_pair_count:
        raise ValueError(
            f"ground truth has {len(ground_truth.pairs)} pairs, expected {spec.expected_pair_count}"
        )
    project_summaries: list[dict[str, Any]] = []
    for project in spec.projects:
        collection = normalize_collection(project.collection, project.name)
        pool = build_pair_pool(
            ground_truth,
            collection,
            project.resources,
            cost_column=spec.cost_column,
            fingerprint_bits=spec.fingerprint_bits,
            cost_unit=spec.cost_unit,
        )
        project_summaries.append(
            {
                "project": project.name,
                "collection": str(project.collection),
                "collection_rows": len(collection.native_rows),
                "collection_pairs": len(collection.pairs),
                "labeled_pairs": sum(row["has_valid_training_label"] == "true" for row in pool),
                "unlabeled_pairs_retained": sum(row["has_valid_training_label"] == "false" for row in pool),
                "resource_rows": len(_read_csv(project.resources)),
                "missing_cost_pairs": sum(row["cost_was_missing"] == "true" for row in pool),
            }
        )
    return {
        "ground_truth_collection": str(spec.ground_truth_collection),
        "ground_truth_rows": len(ground_truth.native_rows),
        "ground_truth_pairs": len(ground_truth.pairs),
        "pair_batch_size": spec.pair_batch_size,
        "round_count": spec.round_count,
        "uncertainty_aggregation": spec.uncertainty_aggregation,
        "diversity_weight": spec.diversity_weight,
        "fingerprint_bits": spec.fingerprint_bits,
        "strategies": list(spec.strategies),
        "seeds": list(spec.seeds),
        "fixed_seed": spec.fixed_seed,
        "trajectory_count": len(spec.projects) * len(spec.strategies) * len(spec.seeds),
        "cost_column": spec.cost_column,
        "cost_unit": spec.cost_unit,
        "cost_rule": (
            f"use {spec.cost_column} directly as {spec.cost_unit}; "
            "blank/missing/non-finite values become zero"
        ),
        "projects": project_summaries,
    }


def _validate_source(spec: ExperimentSpec) -> Path:
    source = spec.source_directory
    if source is None:
        raise ValueError(
            "RxnResid source_directory is required for prepare; provide a local checkout "
            f"at revision {spec.source_revision}"
        )
    if not source.is_dir():
        raise ValueError(f"RxnResid source directory does not exist: {source}")
    try:
        revision = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"cannot read RxnResid git revision: {source}") from exc
    if revision != spec.source_revision:
        raise ValueError(
            f"RxnResid source revision mismatch: {revision}, expected {spec.source_revision}"
        )
    return source


def _write_native_collection(path: Path, collection: NormalizedCollection) -> None:
    _write_csv(path, list(NATIVE_COLUMNS), list(collection.native_rows))


def _trajectory_id(project: str, strategy: str, seed: int) -> str:
    return f"{project}__{strategy}__seed-{seed:02d}"


def _worker_launcher() -> str:
    """Render the Bohr entrypoint with an offline-first dependency bootstrap."""

    return """#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# The worker image already contains the scientific runtime.  Put the staged
# source trees on PYTHONPATH first so an editable pip install is not required
# for the normal path and therefore cannot fail because of transient network
# access to a build backend.
export PYTHONPATH="$PWD/rxnresid/src:$PWD/tricycle-sampling/src:$PWD/tricycle-core/src:${PYTHONPATH:-}"

runtime_ready() {
  python3.12 - <<'PY'
import accelerate
import rdkit
import torch
import torch_geometric
import yaml
import rxnresid.data.dataset
PY
}

if ! runtime_ready; then
  echo "[bootstrap] staged source import check failed; trying editable install" >&2
  export PIP_DISABLE_PIP_VERSION_CHECK=1
  export PIP_DEFAULT_TIMEOUT=120
  export PIP_RETRIES=5
  install_attempts=5
  install_delay=5
  installed=0
  for attempt in $(seq 1 "$install_attempts"); do
    echo "[bootstrap] install attempt ${attempt}/${install_attempts}" >&2
    pip_install=(python3.12 -m pip install --disable-pip-version-check --no-deps --retries 5 --timeout 120)
    if "${pip_install[@]}" --no-build-isolation -e rxnresid || "${pip_install[@]}" -e rxnresid; then
      if runtime_ready; then
        installed=1
        break
      fi
      echo "[bootstrap] install completed but runtime check still fails" >&2
    else
      echo "[bootstrap] install attempt ${attempt} failed" >&2
    fi
    if (( attempt < install_attempts )); then
      echo "[bootstrap] retrying in ${install_delay} seconds" >&2
      sleep "$install_delay"
      if (( install_delay < 60 )); then
        install_delay=$((install_delay * 2))
      fi
    fi
  done
  if (( installed == 0 )); then
    echo "[bootstrap] dependency bootstrap failed after ${install_attempts} attempts" >&2
    exit 1
  fi
fi

exec python3.12 runner.py --request "$1"
"""


def _bohr_request(
    spec: ExperimentSpec,
    *,
    trajectory_id: str,
    project: str,
    strategy: str,
    seed: int,
    input_directory: Path,
    trajectory_config: Path,
) -> dict[str, object]:
    output_location = f"outputs/{trajectory_id}"
    # Bohrium's legacy ``backward_files`` field is a file allow-list.  It is
    # not a recursive glob, and using ``outputs/<trajectory>/**`` can leave a
    # completed job with an empty result archive.  Keep the selectors
    # A trailing-slash directory selector asks Bohrium to retain the complete
    # output tree.  ``<dir>/**`` is not equivalent on the legacy job API and
    # can produce an empty result archive.
    backward_files = [f"{output_location}/"]
    parameters: dict[str, object] = {
        "input_directory": str(input_directory),
        "model_name": "rxnresid",
        "model_family": "sar",
        "task": "active-learning-trajectory",
        "source_repository": spec.source_repository,
        "source_revision": spec.source_revision,
        "trajectory_id": trajectory_id,
        "project": project,
        "strategy": strategy,
        "seed": seed,
        "fixed_seed": spec.fixed_seed,
        "pair_batch_size": spec.pair_batch_size,
        "round_count": spec.round_count,
        "cost_column": spec.cost_column,
        "cost_unit": spec.cost_unit,
        "uncertainty_aggregation": spec.uncertainty_aggregation,
        "training_on_bohr": True,
        "trajectory_config": str(trajectory_config.relative_to(input_directory)),
        "backward_files": backward_files,
        # The user explicitly authorized creation of these billable Bohr jobs;
        # pass the CLI's per-submit confirmation without changing global Bohr
        # billing settings.
        "extra_submit_args": ["--yes"],
    }
    if spec.max_run_time is not None:
        parameters["max_run_time"] = spec.max_run_time
    return {
        "job_name": trajectory_id,
        "operation": "rxnresid.active-learning",
        "image": spec.bohr_image,
        "command": ["bash", "run.sh", str(trajectory_config.relative_to(input_directory))],
        "project_id": spec.bohr_project_id,
        "machine_type": spec.bohr_machine_type,
        "input_artifacts": [
            {
                "artifact_id": f"{trajectory_id}-input",
                "location": str(input_directory),
                "kind": "directory",
            }
        ],
        "output_artifacts": [
            {
                "artifact_id": f"{trajectory_id}-output",
                "location": output_location,
                "kind": "directory",
            }
        ],
        "parameters": parameters,
        "tags": ["tricycle", "rxnresid", "active-learning", project, strategy],
    }


def _validate_bohr_request(request: Mapping[str, object]) -> dict[str, object]:
    """Validate through the generic Bohr request contract when packages are importable."""

    for package_path in (
        REPO_ROOT / "packages" / "tricycle-core" / "src",
        REPO_ROOT / "packages" / "tricycle-bohr" / "src",
    ):
        if str(package_path) not in sys.path:
            sys.path.insert(0, str(package_path))
    try:
        from tricycle_bohr import BohrComputeRequest
    except ImportError as exc:
        raise ValueError("tricycle-bohr is required to validate Bohr requests") from exc
    job = BohrComputeRequest.from_mapping(request).to_job_spec()
    return job.to_dict()


def prepare_experiment(
    spec: ExperimentSpec,
    *,
    output_root: Path | None = None,
    source_directory: Path | None = None,
    machine_type: str | None = None,
) -> dict[str, Any]:
    """Materialize all inputs and one dry-run Bohr request per trajectory."""

    ground_truth = normalize_collection(spec.ground_truth_collection, "ground-truth")
    if len(ground_truth.pairs) != spec.expected_pair_count:
        raise ValueError(
            f"ground truth has {len(ground_truth.pairs)} pairs, expected {spec.expected_pair_count}"
        )
    source_spec = spec
    if source_directory is not None:
        source_spec = replace(spec, source_directory=source_directory.resolve())
    source = _validate_source(source_spec)
    target_root = (output_root or spec.output_root).resolve()
    if target_root.exists() and any(target_root.iterdir()):
        raise ValueError(f"output directory is not empty: {target_root}")
    target_root.mkdir(parents=True, exist_ok=True)
    effective_machine = machine_type or spec.bohr_machine_type
    if not effective_machine.strip():
        raise ValueError("Bohr machine_type must not be empty")

    runner_source = REPO_ROOT / "tools" / "rxnresid_active_learning_runner.py"
    if not runner_source.is_file():
        raise ValueError(f"worker runner is missing: {runner_source}")
    sampling_source = REPO_ROOT / "packages" / "tricycle-sampling" / "src" / "tricycle_sampling"
    core_source = REPO_ROOT / "packages" / "tricycle-core" / "src" / "tricycle_core"
    if not sampling_source.is_dir() or not core_source.is_dir():
        raise ValueError("TriCycle sampling/core source packages are missing")

    source_manifest: dict[str, Any] = {
        "schema": "tricycle.rxnresid-active-learning/v1",
        "config": str(spec.config_path),
        "source_repository": spec.source_repository,
        "source_revision": spec.source_revision,
        "bohr_image": spec.bohr_image,
        "bohr_project_id": spec.bohr_project_id,
        "bohr_machine_type": effective_machine,
        "pair_batch_size": spec.pair_batch_size,
        "round_count": spec.round_count,
        "strategies": list(spec.strategies),
        "seeds": list(spec.seeds),
        "fixed_seed": spec.fixed_seed,
        "diversity_weight": spec.diversity_weight,
        "fingerprint_bits": spec.fingerprint_bits,
        "ground_truth_rows": len(ground_truth.native_rows),
        "ground_truth_pairs": len(ground_truth.pairs),
        "trajectories": [],
    }
    shutil.copy2(spec.config_path, target_root / "experiment-config.toml")
    _write_native_collection(target_root / "ground-truth.csv", ground_truth)

    trajectory_manifest_rows: list[dict[str, object]] = []
    for project in spec.projects:
        normalized_project = normalize_collection(project.collection, project.name)
        pool = build_pair_pool(
            ground_truth,
            normalized_project,
            project.resources,
            cost_column=spec.cost_column,
            fingerprint_bits=spec.fingerprint_bits,
            cost_unit=spec.cost_unit,
        )
        project_root = target_root / "bohr-input" / project.name
        project_root.mkdir(parents=True, exist_ok=True)
        _copy_tree(source, project_root / "rxnresid")
        _copy_tree(sampling_source, project_root / "tricycle-sampling" / "src" / "tricycle_sampling")
        _copy_tree(core_source, project_root / "tricycle-core" / "src" / "tricycle_core")
        # The worker runner defines experiment behavior and must be a stable
        # snapshot once this bundle has been prepared.
        shutil.copy2(runner_source, project_root / "runner.py")
        _write_native_collection(project_root / "common" / "ground-truth.csv", ground_truth)
        _write_native_collection(project_root / "common" / "project-labels.csv", normalized_project)
        _write_csv(
            project_root / "common" / "pair-pool.csv",
            list(pool[0]),
            pool,
        )
        (project_root / "run.sh").write_text(_worker_launcher(), encoding="utf-8")
        (project_root / "run.sh").chmod(0o755)

        project_pairs = set(normalized_project.pairs)
        for strategy in spec.strategies:
            for seed in spec.seeds:
                trajectory = _trajectory_id(project.name, strategy, seed)
                trajectory_dir = project_root / "trajectories" / trajectory
                trajectory_dir.mkdir(parents=True, exist_ok=True)
                trajectory_config = trajectory_dir / "trajectory.toml"
                _write_toml(
                    trajectory_config,
                    {
                        "schema": "tricycle.rxnresid-active-learning-trajectory/v1",
                        "trajectory_id": trajectory,
                        "project": project.name,
                        "strategy": strategy,
                        "seed": seed,
                        "fixed_seed": spec.fixed_seed,
                        "pair_batch_size": spec.pair_batch_size,
                        "round_count": spec.round_count,
                        "pair_count": len(pool),
                        "uncertainty_aggregation": spec.uncertainty_aggregation,
                        "diversity_weight": spec.diversity_weight,
                        "fingerprint_bits": spec.fingerprint_bits,
                        "ground_truth": "common/ground-truth.csv",
                        "project_labels": "common/project-labels.csv",
                        "pair_pool": "common/pair-pool.csv",
                        "output_dir": f"outputs/{trajectory}",
                        "cost_column": spec.cost_column,
                        "cost_unit": spec.cost_unit,
                        "source_repository": spec.source_repository,
                        "source_revision": spec.source_revision,
                        "training_epochs": spec.training_epochs,
                        "training_batch_size_paths": spec.training_batch_size_paths,
                        "training_device": spec.training_device,
                        "training_mixed_precision": spec.training_mixed_precision,
                        "persist_round_predictions": spec.persist_predictions,
                    },
                )
                request = _bohr_request(
                    source_spec,
                    trajectory_id=trajectory,
                    project=project.name,
                    strategy=strategy,
                    seed=seed,
                    input_directory=project_root,
                    trajectory_config=trajectory_config,
                )
                request["machine_type"] = effective_machine
                request_path = target_root / "requests" / f"{trajectory}.toml"
                _write_toml(request_path, request)
                job_dict = _validate_bohr_request(request)
                plan_path = target_root / "plans" / f"{trajectory}.json"
                plan_path.parent.mkdir(parents=True, exist_ok=True)
                plan_path.write_text(
                    json.dumps(job_dict, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                row = {
                    "trajectory_id": trajectory,
                    "project": project.name,
                    "strategy": strategy,
                    "seed": seed,
                    "fixed_seed": spec.fixed_seed,
                    "pair_count": len(pool),
                    "project_labeled_pair_count": len(project_pairs),
                    "pair_batch_size": spec.pair_batch_size,
                    "round_count": spec.round_count,
                    "machine_type": effective_machine,
                    "image": spec.bohr_image,
                    "request": str(request_path.relative_to(target_root)),
                    "bohr_input_directory": str(project_root),
                    "plan": str(plan_path.relative_to(target_root)),
                    "status": "prepared",
                }
                trajectory_manifest_rows.append(row)
                source_manifest["trajectories"].append(row)

    _write_csv(
        target_root / "trajectory-manifest.tsv",
        list(trajectory_manifest_rows[0]),
        trajectory_manifest_rows,
        delimiter="\t",
    )
    (target_root / "manifest.json").write_text(
        json.dumps(source_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (target_root / "SOURCE.txt").write_text(
        f"ground_truth_collection={spec.ground_truth_collection}\n"
        f"source_repository={spec.source_repository}\n"
        f"source_revision={spec.source_revision}\n"
        f"bohr_machine_type={effective_machine}\n"
        f"randomness_rule=trajectory seed selects only the first batch; later acquisition ties use candidate IDs; random-baseline ordering uses a fixed candidate-ID hash with fixed_seed={spec.fixed_seed}; training uses fixed_seed={spec.fixed_seed}\n"
        f"training_determinism=RxnResid seeds Python, NumPy, and PyTorch and requires deterministic PyTorch algorithms\n"
        f"cost_rule=use {spec.cost_column} directly as {spec.cost_unit}; missing=0\n",
        encoding="utf-8",
    )
    return {
        "output_root": str(target_root),
        "trajectory_count": len(trajectory_manifest_rows),
        "pair_count": len(ground_truth.pairs),
        "round_count": spec.round_count,
        "machine_type": effective_machine,
        "request_directory": str(target_root / "requests"),
        "manifest": str(target_root / "trajectory-manifest.tsv"),
    }


def _read_curve(path: Path) -> list[dict[str, str]]:
    rows = _read_csv(path)
    required = {
        "trajectory_id",
        "project",
        "strategy",
        "seed",
        "total_selected_pairs",
        "cumulative_sampling_cost_core_hours",
        "full_space_mae_kcal_mol",
    }
    if rows:
        missing = sorted(required - set(rows[0]))
        if missing:
            raise ValueError(f"{path}: curve is missing columns: {', '.join(missing)}")
    return rows


def collect_results(root: Path, *, allow_missing: bool = False) -> dict[str, object]:
    """Aggregate completed worker curves into analysis-ready CSV files."""

    curve_paths = sorted(root.rglob("curve.csv"))
    if not curve_paths:
        raise ValueError(f"no curve.csv found below {root}")
    curves: list[dict[str, str]] = []
    seen_trajectories: set[str] = set()
    for path in curve_paths:
        rows = _read_curve(path)
        if not rows:
            raise ValueError(f"empty curve: {path}")
        trajectory = rows[0]["trajectory_id"]
        if trajectory in seen_trajectories:
            raise ValueError(f"duplicate trajectory curve: {trajectory}")
        seen_trajectories.add(trajectory)
        curves.extend(rows)
    curves.sort(
        key=lambda row: (
            row["project"],
            row["strategy"],
            int(row["seed"]),
            int(row["total_selected_pairs"]),
        )
    )
    result_dir = root / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(result_dir / "curves.csv", list(curves[0]), curves)

    grouped: defaultdict[tuple[str, str, int], list[dict[str, str]]] = defaultdict(list)
    for row in curves:
        grouped[(row["project"], row["strategy"], int(row["total_selected_pairs"]))].append(row)
    summary: list[dict[str, object]] = []
    for (project, strategy, budget), rows in sorted(grouped.items()):
        mae_values = [float(row["full_space_mae_kcal_mol"]) for row in rows if row["full_space_mae_kcal_mol"]]
        cost_values = [float(row["cumulative_sampling_cost_core_hours"]) for row in rows]
        if not mae_values and not allow_missing:
            raise ValueError(f"{project}/{strategy}/{budget}: all MAE values are missing")
        mae_mean = sum(mae_values) / len(mae_values) if mae_values else ""
        mae_std = (
            math.sqrt(sum((value - float(mae_mean)) ** 2 for value in mae_values) / len(mae_values))
            if mae_values
            else ""
        )
        summary.append(
            {
                "project": project,
                "strategy": strategy,
                "total_selected_pairs": budget,
                "seed_count": len(rows),
                "mae_count": len(mae_values),
                "full_space_mae_mean_kcal_mol": mae_mean,
                "full_space_mae_std_kcal_mol": mae_std,
                "cumulative_sampling_cost_mean_core_hours": sum(cost_values) / len(cost_values),
                "cumulative_sampling_cost_min_core_hours": min(cost_values),
                "cumulative_sampling_cost_max_core_hours": max(cost_values),
            }
        )
    _write_csv(result_dir / "curve-summary.csv", list(summary[0]), summary)
    return {
        "root": str(root),
        "curve_files": len(curve_paths),
        "trajectories": len(seen_trajectories),
        "curve_rows": len(curves),
        "curves_csv": str(result_dir / "curves.csv"),
        "summary_csv": str(result_dir / "curve-summary.csv"),
    }


def _main_validate(args: argparse.Namespace) -> int:
    spec = load_experiment_spec(args.config, repo_root=args.repo_root)
    print(json.dumps(validate_inputs(spec), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _main_prepare(args: argparse.Namespace) -> int:
    spec = load_experiment_spec(args.config, repo_root=args.repo_root)
    result = prepare_experiment(
        spec,
        output_root=args.output_root,
        source_directory=args.rxnresid_source,
        machine_type=args.machine_type,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _main_collect(args: argparse.Namespace) -> int:
    result = collect_results(args.root, allow_missing=args.allow_missing)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _nested_value(value: object, keys: tuple[str, ...]) -> object | None:
    if isinstance(value, Mapping):
        for key in keys:
            if key in value:
                return value[key]
        for child in value.values():
            found = _nested_value(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _nested_value(child, keys)
            if found is not None:
                return found
    return None


def _main_submit(args: argparse.Namespace) -> int:
    if not args.live:
        raise ValueError("batch submission requires --live")
    root = args.root.resolve()
    manifest_path = root / "trajectory-manifest.tsv"
    if not manifest_path.is_file():
        raise ValueError(f"prepared trajectory manifest not found: {manifest_path}")
    rows = _read_csv(manifest_path)
    if not rows:
        raise ValueError("prepared trajectory manifest is empty")
    expected_trajectories = len(PROJECTS) * len({row["strategy"] for row in rows}) * 5
    if len(rows) != expected_trajectories:
        raise ValueError(
            "trajectory manifest must contain five seeds for every project/strategy pair; "
            f"got {len(rows)} rows for {expected_trajectories} expected rows"
        )
    requested_trajectories = {str(value).strip() for value in (args.trajectory or ()) if str(value).strip()}
    known_trajectories = {row["trajectory_id"] for row in rows}
    unknown_trajectories = requested_trajectories - known_trajectories
    if unknown_trajectories:
        raise ValueError(
            "unknown trajectory selection: " + ", ".join(sorted(unknown_trajectories))
        )
    if requested_trajectories:
        rows = [row for row in rows if row["trajectory_id"] in requested_trajectories]
    if not rows:
        raise ValueError("trajectory selection is empty")
    submission_root = root / "submissions"
    submission_root.mkdir(parents=True, exist_ok=True)
    group_state_path = root / "job-group.json"
    if group_state_path.is_file():
        with group_state_path.open("rb") as handle:
            group_state = tomllib.load(handle) if group_state_path.suffix == ".toml" else json.load(handle)
        job_group_id = str(group_state.get("job_group_id", "")).strip()
        if not job_group_id:
            raise ValueError(f"job group state has no job_group_id: {group_state_path}")
        if args.job_group_id and str(args.job_group_id).strip() != job_group_id:
            raise ValueError(
                f"requested job group {args.job_group_id} conflicts with {job_group_id}"
            )
        job_group_name = str(group_state.get("job_group_name", args.job_group_name or root.name))
    else:
        try:
            from tricycle_bohrsubmit import BohrCliGateway
        except ImportError as exc:
            raise ValueError("tricycle-bohrsubmit is required to create the common Bohr job group") from exc
        project_id = str(args.project_id or DEFAULT_PROJECT_ID)
        job_group_name = args.job_group_name or root.name
        job_group_id = str(args.job_group_id or "").strip()
        if not job_group_id:
            job_group_id = str(
                BohrCliGateway().create_job_group(job_group_name, project_id)
            ).strip()
        if not job_group_id:
            raise ValueError("Bohr returned an empty job group id")
        group_state = {
            "schema": "tricycle.rxnresid-active-learning-job-group/v1",
            "job_group_id": job_group_id,
            "job_group_name": job_group_name,
            "project_id": project_id,
            "trajectory_count": len(known_trajectories),
        }
        (root / "job-group.json").write_text(
            json.dumps(group_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    grouped_request_root = root / "grouped-requests"
    grouped_plan_root = root / "grouped-plans"
    result_rows: list[dict[str, object]] = []
    failed_trajectories: list[str] = []
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("uv is required to submit through tricycle-agent")
    for row in rows:
        trajectory = row["trajectory_id"]
        request_path = root / row["request"]
        if not request_path.is_file():
            raise ValueError(f"request listed in manifest is missing: {request_path}")
        with request_path.open("rb") as handle:
            grouped_request = tomllib.load(handle)
        parameters = grouped_request.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ValueError(f"request parameters must be a table: {request_path}")
        parameters["job_group_id"] = job_group_id
        grouped_request["parameters"] = parameters
        if args.job_name_suffix:
            grouped_request["job_name"] = (
                f"{grouped_request.get('job_name', trajectory)}-{args.job_name_suffix}"
            )
        grouped_request_path = grouped_request_root / request_path.name
        _write_toml(grouped_request_path, grouped_request)
        grouped_plan = _validate_bohr_request(grouped_request)
        grouped_plan_path = grouped_plan_root / f"{trajectory}.json"
        grouped_plan_path.parent.mkdir(parents=True, exist_ok=True)
        grouped_plan_path.write_text(
            json.dumps(grouped_plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        run_dir = submission_root / trajectory
        existing_state = run_dir / "run.toml"
        if existing_state.is_file():
            with existing_state.open("rb") as handle:
                state = tomllib.load(handle)
            external_id = _nested_value(state, ("external_id", "externalId"))
            if external_id:
                result_rows.append(
                    {
                        "trajectory_id": trajectory,
                        "request": row["request"],
                        "status": "already-submitted",
                        "return_code": 0,
                        "external_id": str(external_id),
                        "job_group_id": job_group_id,
                        "run_dir": str(run_dir),
                        "log": str(run_dir / "submit.log"),
                    }
                )
                continue
        run_id_suffix = f"-{args.job_name_suffix}" if args.job_name_suffix else ""
        run_id = f"{trajectory}{run_id_suffix}-submit"
        command = [
            uv,
            "run",
            "--package",
            "tricycle-agent",
            "tricycle-agent",
            "run",
            "bohr.compute.submit",
            str(grouped_request_path),
            "--run-id",
            run_id,
            "--run-dir",
            str(run_dir),
            "--live",
        ]
        run_dir.mkdir(parents=True, exist_ok=True)
        log_path = run_dir / "submit.log"
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                command,
                cwd=REPO_ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
                env=os.environ.copy(),
            )
        external_id = ""
        if existing_state.is_file():
            with existing_state.open("rb") as handle:
                state = tomllib.load(handle)
            found = _nested_value(state, ("external_id", "externalId"))
            if found:
                external_id = str(found)
        status = "submitted" if completed.returncode == 0 and external_id else "failed"
        result_rows.append(
            {
                "trajectory_id": trajectory,
                "request": row["request"],
                "status": status,
                "return_code": completed.returncode,
                "external_id": external_id,
                "job_group_id": job_group_id,
                "run_dir": str(run_dir),
                "log": str(log_path),
            }
        )
        _write_csv(
            root / "submission-results.tsv",
            list(result_rows[0]),
            result_rows,
            delimiter="\t",
        )
        if status == "failed":
            failed_trajectories.append(trajectory)
    _write_csv(
        root / "submission-results.tsv",
        list(result_rows[0]),
        result_rows,
        delimiter="\t",
    )
    print(
        json.dumps(
            {
                "root": str(root),
                "submitted_or_reused": len(result_rows),
                "job_group_id": job_group_id,
                "job_group_name": job_group_name,
                "submission_results": str(root / "submission-results.tsv"),
                "failed_trajectories": failed_trajectories,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if failed_trajectories else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate exports and the trajectory matrix")
    validate.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    validate.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    validate.set_defaults(func=_main_validate)

    prepare = subparsers.add_parser(
        "prepare",
        help="stage local bundles and one dry-run Bohr request per trajectory",
    )
    prepare.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    prepare.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    prepare.add_argument("--output-root", type=Path, default=None)
    prepare.add_argument("--rxnresid-source", type=Path, default=None)
    prepare.add_argument("--machine-type", default=None)
    prepare.set_defaults(func=_main_prepare)

    collect = subparsers.add_parser("collect", help="aggregate completed worker curves")
    collect.add_argument("--root", type=Path, required=True)
    collect.add_argument("--allow-missing", action="store_true")
    collect.set_defaults(func=_main_collect)

    submit = subparsers.add_parser(
        "submit",
        help="submit all prepared trajectories through tricycle-agent",
    )
    submit.add_argument("--root", type=Path, required=True)
    submit.add_argument(
        "--live",
        action="store_true",
        help="required confirmation that remote Bohr jobs may be created",
    )
    submit.add_argument(
        "--job-group-name",
        default=None,
        help="name for the one common Bohr job group (defaults to the prepared root name)",
    )
    submit.add_argument(
        "--project-id",
        default=None,
        help="Bohr project id used when creating the common job group",
    )
    submit.add_argument(
        "--job-group-id",
        default=None,
        help="reuse an existing Bohr job group instead of creating a new one",
    )
    submit.add_argument(
        "--trajectory",
        action="append",
        default=None,
        help="submit only this trajectory; repeat the option for multiple trajectories",
    )
    submit.add_argument(
        "--job-name-suffix",
        default=None,
        help="append a suffix to submitted job names and local submit run ids",
    )
    submit.set_defaults(func=_main_submit)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
