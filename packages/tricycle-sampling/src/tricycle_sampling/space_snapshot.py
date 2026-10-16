"""Freeze database-filtered labeled datasets without local row selection."""
from __future__ import annotations

import csv
import json
import math
import os
import shutil
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from tricycle_core import timestamped_run_directory

WORKDIR = Path.cwd().resolve()
CONTRACT = "tricycle.frozen-sampling-space.v3"
GT_PROJECT_ID = "00000000-0000-7000-8000-000000000201"


from tricycle_core.io import file_sha256 as _hash  # noqa: E402


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    path.relative_to(root)
    return path


def _validate_source(root: Path) -> tuple[dict[str, Any], Path, list[dict[str, str]], dict[str, Any]]:
    source = json.loads((root / "manifest.json").read_text())
    registry = Path(source["substrate_registry"])
    structures: dict[str, dict[str, str]] = {"ene": {}, "diene": {}}
    for row in _rows(registry):
        for role in structures:
            ident = str(int(row[f"{role}_id"].split(".")[0])).zfill(12)
            structures[role].setdefault(ident, row[f"{role}_smiles"])
    expected = {(e, d) for e in structures["ene"] for d in structures["diene"]}
    safe_registry = [
        {"ene_id": e, "ene_smiles": structures["ene"][e],
         "diene_id": d, "diene_smiles": structures["diene"][d]}
        for e, d in sorted(expected)
    ]
    pools = {}
    evaluation_count = 0
    for project in source["projects"]:
        name, role, pid = project["name"], project["role"], project["project_id"]
        if role not in {"candidate", "evaluation"} or (
            (role == "evaluation") != (pid == GT_PROJECT_ID)
        ):
            raise ValueError("snapshot project roles must isolate ground truth")
        if name in pools:
            raise ValueError("duplicate snapshot pool")
        relative = f"{'evaluation' if role == 'evaluation' else 'candidates'}/{name}"
        folder = _inside(root, relative)
        raw, adapted = folder / "database-export.csv", folder / "reaction-collection.csv"
        receipt = json.loads((folder / "database-export.receipt.json").read_text())
        adaptation = json.loads((folder / "adaptation.json").read_text())
        if receipt.get("arguments", {}).get("has_activation_gibbs_free_energy") is not True:
            raise ValueError(f"source requires the native database activation-label filter: {name}")
        if (
            receipt["project_id"] != pid or adaptation["project_id"] != pid
            or _hash(raw) != receipt["sha256"] or _hash(raw) != adaptation["source_sha256"]
            or _hash(adapted) != adaptation["sha256"]
            or _hash(registry) != adaptation["substrate_registry_sha256"]
        ):
            raise ValueError(f"source hashes or project proof changed: {name}")
        native, rows = _rows(raw), _rows(adapted)
        if not native or len(native) != len(rows) or len(rows) != receipt["row_count"]:
            raise ValueError(f"source row count changed: {name}")
        if sorted(int(row["export_row_index"]) for row in rows) != list(range(len(native))):
            raise ValueError(f"source profile rows were expanded or lost: {name}")
        labeled_pairs = set()
        labeled_rows = 0
        for row in rows:
            if {key: row[key] for key in native[0]} != native[int(row["export_row_index"])]:
                raise ValueError(f"native fields changed: {name}")
            label = row["activation_gibbs_free_energy_kcal_mol"]
            if row["activation_free_energy_kcal_mol"] != label:
                raise ValueError(f"adapted label changed: {name}")
            if not label.strip():
                raise ValueError(f"database-filtered source contains an unlabeled row: {name}")
            if label.strip():
                pair = (row["ene_id"], row["diene_id"])
                if not math.isfinite(float(label)) or row["substrate_match_status"] != "matched":
                    raise ValueError(f"label is invalid or lacks a unique stereo pair: {name}")
                if pair not in expected or row["pair_id"] != "+".join(pair):
                    raise ValueError(f"label pair is outside registry: {name}")
                labeled_rows += 1
                labeled_pairs.add(pair)
        if role == "evaluation":
            evaluation_count += 1
            if labeled_pairs != expected:
                raise ValueError("ground truth does not cover the complete registry Cartesian product")
        pools[name] = {
            "role": role, "project_id": pid, "collection": f"{relative}/reaction-collection.csv",
            "native_export": f"{relative}/database-export.csv", "rows": len(rows),
            "labeled_rows": labeled_rows, "labeled_pairs": len(labeled_pairs),
            "substrate_match_policy": adaptation["substrate_match_policy"],
        }
    if evaluation_count != 1:
        raise ValueError("snapshot requires exactly one ground-truth pool")
    return source, registry, safe_registry, pools


def verify_frozen_space(config_path: Path) -> dict[str, Any]:
    """Verify the pinned manifest and every archived file, entirely offline."""
    config_path = config_path.resolve()
    pin = tomllib.loads(config_path.read_text())["snapshot"]
    manifest_path = (config_path.parent / pin["manifest"]).resolve()
    if _hash(manifest_path) != pin["manifest_sha256"]:
        raise ValueError("frozen sampling-space manifest hash changed")
    manifest = json.loads(manifest_path.read_text())
    if manifest["contract"] not in {CONTRACT, "tricycle.frozen-sampling-space.v1", "tricycle.frozen-sampling-space.v2"} or manifest["snapshot_id"] != pin["id"]:
        raise ValueError("frozen sampling-space identity changed")
    root = manifest_path.parent
    for relative, proof in manifest["files"].items():
        path = _inside(root, relative)
        if path.stat().st_size != proof["bytes"] or _hash(path) != proof["sha256"]:
            raise ValueError(f"frozen sampling-space file changed: {relative}")
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
    if actual != set(manifest["files"]) | {manifest_path.name}:
        raise ValueError("frozen sampling-space file inventory changed")
    return {"root": str(root), "manifest_sha256": pin["manifest_sha256"], **manifest}


def freeze_database_space(
    source_root: Path, pin_config: Path, *, snapshot_id: str, output_root: Path | None = None,
) -> dict[str, Any]:
    """Pin the exact database-filtered export; do not filter or rewrite rows."""
    source_root, pin_config = source_root.resolve(), pin_config.resolve()
    if not snapshot_id or Path(snapshot_id).name != snapshot_id or snapshot_id in {".", ".."}:
        raise ValueError("snapshot ID must be a directory name")
    if pin_config.exists():
        raise ValueError("snapshot pin already exists; do not replace a frozen space")
    source, registry, safe_registry, pools = _validate_source(source_root)
    root = (output_root or timestamped_run_directory(WORKDIR / "artifacts" / "datasets", snapshot_id)).resolve()
    if root == source_root or source_root in root.parents or root in source_root.parents:
        raise ValueError("snapshot output must be independent of its source")
    root.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source_root, root / "audit" / "source-export")
    statistics = []
    for name, pool in pools.items():
        relative = pool["collection"]
        original = source_root / relative
        rows = _rows(original)
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        pool["native_rows"] = pool["rows"]
        pool["unlabeled_rows"] = 0
        pool["adapted_source"] = f"audit/source-export/{relative}"
        pool["native_export"] = f"audit/source-export/{pool['native_export']}"
        pool["label_policy"] = "database-has-activation-gibbs-free-energy-v1"
        statistics.append({"pool": name, "source_rows": len(rows), "dataset_rows": len(rows),
                           "unlabeled_rows": 0, "local_rows_removed": 0,
                           "labeled_pairs": pool["labeled_pairs"]})
    with (root / "dataset-statistics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(statistics[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(statistics)
    (root / "summary.md").write_text(
        "# Frozen labeled sampling space\n\n"
        "The database export used has_activation_gibbs_free_energy=true; no local row filtering or label reconstruction is performed.\n"
        "The active collection is byte-identical to the complete adapted native export; all values and export_row_index are unchanged.\n"
        "Evidence for this labeled native export is retained in audit/source-export.\n\n"
        "| Pool | Source rows | Active rows | Unlabeled rows | Labeled pairs |\n"
        "| --- | ---: | ---: | ---: | ---: |\n"
        + "".join(f"| {r['pool']} | {r['source_rows']} | {r['dataset_rows']} | "
                  f"{r['unlabeled_rows']} | {r['labeled_pairs']} |\n" for r in statistics),
        encoding="utf-8",
    )
    for filename in ("gt-pair-coverage.csv", "gt-missing-pairs.csv"):
        if (source_root / filename).is_file():
            shutil.copyfile(source_root / filename, root / filename)
    provenance = root / "evaluation" / "provenance"
    provenance.mkdir(parents=True)
    shutil.copyfile(registry, provenance / "substrate-registry-source.csv")
    registry_path = root / "substrate-registry.csv"
    with registry_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(safe_registry[0]))
        writer.writeheader()
        writer.writerows(safe_registry)
    (root / "FROZEN.md").write_text(
        f"# {snapshot_id}\n\n"
        "Frozen native database profiles and substrate stereo assignments. "
        "Use the pinned configuration and verify hashes before a new experiment; "
        "do not resolve a latest export or refresh this snapshot from the database.\n\n"
        "`substrate-registry.csv` contains only IDs and structures. The original registry "
        "with historical QM fields is retained solely under evaluation/provenance. "
        "Acquisition receives candidate data and structures; ground-truth labels and "
        "evaluation results remain isolated.\n\n"
        "The native database export applies has_activation_gibbs_free_energy=true. "
        "No local row filtering is performed. Every filtered native row is retained "
        "under audit/source-export and in its adapted active collection. "
        "Labeled profiles remain independent; no minimum/mean label policy "
        "or shared-TS deduplication is applied. Native phase times are preserved; "
        "the complete core-hour cost ledger must be pinned separately for an experiment.\n",
        encoding="utf-8",
    )
    manifest = {
        "contract": CONTRACT, "snapshot_id": snapshot_id,
        "frozen_at": datetime.now().astimezone().isoformat(), "source_export": str(source_root),
        "expected_pair_count": len(safe_registry), "substrate_registry": registry_path.name,
        "substrate_identity_basis": "exported reactant stereo graphs; no TS input origin",
        "profile_policy": "preserve every database-filtered profile; no local selection or aggregation",
        "native_profile_policy": source["profile_policy"],
        "label_policy": "database-has-activation-gibbs-free-energy-v1", "pools": pools,
        "files": {
            str(path.relative_to(root)): {"sha256": _hash(path), "bytes": path.stat().st_size}
            for path in sorted(root.rglob("*")) if path.is_file()
        },
    }
    manifest_path = root / "frozen-space.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    pin_config.parent.mkdir(parents=True, exist_ok=True)
    with pin_config.open("x", encoding="utf-8") as handle:
        handle.write(
            "# Fixed sampling space; paths resolve relative to this file.\n[snapshot]\n"
            f"id = {json.dumps(snapshot_id)}\n"
            f"manifest = {json.dumps(os.path.relpath(manifest_path, pin_config.parent))}\n"
            f"manifest_sha256 = {json.dumps(_hash(manifest_path))}\n"
        )
    return verify_frozen_space(pin_config)
