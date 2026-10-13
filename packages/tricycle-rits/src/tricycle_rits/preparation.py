"""Prepare native RitS demo inputs with verified sources and independent snapshots."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

from tricycle_core.io import copy_snapshot_file, copy_snapshot_tree, write_delimited

from .assets import load_model_asset


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _require(row: dict[str, str], key: str, *, path: Path) -> str:
    value = row.get(key, "").strip()
    if not value:
        raise ValueError(f"{path}: missing non-empty column {key!r}")
    return value


def _sha1(value: str) -> str:
    return hashlib.sha1(value.encode()).hexdigest()[:16]


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    write_delimited(path, fieldnames, rows)


def _normalize_xyz(value: str, *, source: Path, row_index: int) -> str:
    lines = value.replace("\r\n", "\n").replace("\r", "\n").strip().splitlines()
    if len(lines) < 2:
        raise ValueError(f"{source}: row {row_index} has an incomplete XYZ block")
    try:
        atom_count = int(lines[0].strip())
    except ValueError as exc:
        raise ValueError(f"{source}: row {row_index} has an invalid XYZ atom count") from exc
    if atom_count < 1 or len(lines) != atom_count + 2:
        raise ValueError(
            f"{source}: row {row_index} XYZ count mismatch: "
            f"header={atom_count}, lines={len(lines) - 2}"
        )
    for atom_line in lines[2:]:
        if len(atom_line.split()) < 4:
            raise ValueError(f"{source}: row {row_index} has a malformed XYZ atom line")
    return "\n".join(lines) + "\n"


def prepare_demo(
    *,
    export_path: Path,
    source: Path,
    checkpoint: Path,
    root: Path,
) -> dict[str, object]:
    rows = _read_csv(export_path)
    if not rows:
        raise ValueError(f"RitS export is empty: {export_path}")
    required = ("mapped_reaction_smiles", "ts_structure")
    if any(key not in rows[0] for key in required):
        raise ValueError(f"RitS export must contain columns: {', '.join(required)}")
    if not checkpoint.is_file():
        raise ValueError(f"RitS checkpoint is not a file: {checkpoint}")

    from tricycle_bohr.model_source import validate_source_checkout
    from tricycle_core.io import file_sha256

    asset = load_model_asset()
    validate_source_checkout(source, asset.revision)
    if file_sha256(checkpoint) != asset.checkpoint_sha256:
        raise ValueError("RitS checkpoint hash mismatch")
    copy_snapshot_tree(source, root)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "rits.ckpt").parent.mkdir(parents=True, exist_ok=True)
    copy_snapshot_file(checkpoint, root / "data" / "rits.ckpt")

    reaction_rows: list[dict[str, object]] = []
    metadata_rows: list[dict[str, object]] = []
    reaction_lines: list[str] = []
    xyz_dir = root / "data" / "rits_demo_xyz"
    xyz_dir.mkdir(parents=True, exist_ok=True)
    for index, row in enumerate(rows):
        reaction = _require(row, "mapped_reaction_smiles", path=export_path)
        xyz = _normalize_xyz(
            _require(row, "ts_structure", path=export_path), source=export_path, row_index=index
        )
        reaction_id = row.get("mapped_reaction_id", "") or f"db-demo-{index:04d}"
        ene_id = str(100000 + index)
        diene_id = str(200000 + index)
        prod_id = str(300000 + index)
        digest = _sha1(f"{reaction_id}|{reaction}")
        xyz_name = f"{ene_id}_{diene_id}_{prod_id}_conf_0_ts.{digest}.xyz"
        (xyz_dir / xyz_name).write_text(xyz, encoding="utf-8")
        reaction_rows.append(
            {
                "ene_id": ene_id,
                "diene_id": diene_id,
                "prod_id": prod_id,
                "rxn_smiles": reaction,
                "reaction_id": reaction_id,
            }
        )
        reaction_lines.append(reaction)
        metadata_rows.append(
            {
                "source_row_index": index,
                "mapped_reaction_id": reaction_id,
                "geometry_id": row.get("geometry_id", ""),
                "xyz_file": xyz_name,
                "rxn_smiles": reaction,
            }
        )

    raw_dir = root / "data" / "rits_demo_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    reaction_csv = raw_dir / "reactions.csv"
    _write_csv(
        reaction_csv,
        ["ene_id", "diene_id", "prod_id", "rxn_smiles", "reaction_id"],
        reaction_rows,
    )
    (root / "data" / "demo_reactions.smarts").write_text(
        "\n".join(reaction_lines) + "\n", encoding="utf-8"
    )
    _write_csv(
        root / "data" / "rits_demo_manifest.csv",
        ["source_row_index", "mapped_reaction_id", "geometry_id", "xyz_file", "rxn_smiles"],
        metadata_rows,
    )
    dataset_dir = root / "data" / "rits_demo_dataset"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    (dataset_dir / "SOURCE.txt").write_text(
        f"source_export={export_path}\nsource_repository={asset.repository}\n"
        f"source_revision={asset.revision}\n",
        encoding="utf-8",
    )
    return {
        "input_directory": str(root),
        "reaction_csv": str(reaction_csv),
        "xyz_dir": str(xyz_dir),
        "dataset_dir": str(dataset_dir),
        "reaction_count": len(rows),
    }
