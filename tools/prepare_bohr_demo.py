#!/usr/bin/env python3
"""Build small, auditable RitS and RxnResid Bohr input bundles.

The database export is deliberately kept as the source of truth.  This tool
only adapts its exported columns to the native input contracts of the pinned
upstream repositories and records the source identifiers alongside the
derived files.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import shutil
from collections import defaultdict
from pathlib import Path

RITS_REVISION = "6d8aa0704c21d2798326a7e070491abf438ca292"
RXNRESID_REVISION = "ac2a0c59b80d430564309c77d26f0a0d69139009"


def _link_or_copy(source: str, destination: str) -> None:
    source_path = Path(source)
    destination_path = Path(destination)
    try:
        destination_path.hardlink_to(source_path)
    except OSError:
        shutil.copy2(source_path, destination_path)


def _copy_source(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise SystemExit(f"source repository is not a directory: {source}")
    shutil.copytree(
        source,
        destination,
        copy_function=_link_or_copy,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache"),
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _require(row: dict[str, str], key: str, *, path: Path) -> str:
    value = row.get(key, "").strip()
    if not value:
        raise SystemExit(f"{path}: missing non-empty column {key!r}")
    return value


def _sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in fieldnames} for row in rows)


def _normalize_xyz(value: str, *, source: Path, row_index: int) -> str:
    lines = value.replace("\r\n", "\n").replace("\r", "\n").strip().splitlines()
    if len(lines) < 2:
        raise SystemExit(f"{source}: row {row_index} has an incomplete XYZ block")
    try:
        atom_count = int(lines[0].strip())
    except ValueError as exc:
        raise SystemExit(f"{source}: row {row_index} has an invalid XYZ atom count") from exc
    if atom_count < 1 or len(lines) != atom_count + 2:
        raise SystemExit(
            f"{source}: row {row_index} XYZ count mismatch: "
            f"header={atom_count}, lines={len(lines) - 2}"
        )
    for atom_line in lines[2:]:
        if len(atom_line.split()) < 4:
            raise SystemExit(f"{source}: row {row_index} has a malformed XYZ atom line")
    return "\n".join(lines) + "\n"


def _strip_atom_maps(value: str) -> str:
    return re.sub(r":-?\d+", "", value)


def _component_id(component: str, role: str) -> str:
    return f"{role}-{_sha1(_strip_atom_maps(component))}"


def _build_rits_bundle(
    *,
    export_path: Path,
    source: Path,
    checkpoint: Path,
    root: Path,
) -> dict[str, object]:
    rows = _read_csv(export_path)
    if not rows:
        raise SystemExit(f"RitS export is empty: {export_path}")
    required = ("mapped_reaction_smiles", "ts_structure")
    if any(key not in rows[0] for key in required):
        raise SystemExit(f"RitS export must contain columns: {', '.join(required)}")
    if not checkpoint.is_file():
        raise SystemExit(f"RitS checkpoint is not a file: {checkpoint}")

    _copy_source(source, root)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "rits.ckpt").parent.mkdir(parents=True, exist_ok=True)
    _link_or_copy(str(checkpoint), str(root / "data" / "rits.ckpt"))

    reaction_rows: list[dict[str, object]] = []
    metadata_rows: list[dict[str, object]] = []
    reaction_lines: list[str] = []
    xyz_dir = root / "data" / "rits_demo_xyz"
    xyz_dir.mkdir(parents=True, exist_ok=True)
    for index, row in enumerate(rows):
        reaction = _require(row, "mapped_reaction_smiles", path=export_path)
        xyz = _normalize_xyz(_require(row, "ts_structure", path=export_path), source=export_path, row_index=index)
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
        f"source_export={export_path}\nsource_repository=https://github.com/gentle1999/RitS\n"
        f"source_revision={RITS_REVISION}\n",
        encoding="utf-8",
    )
    return {
        "input_directory": str(root),
        "reaction_csv": str(reaction_csv),
        "xyz_dir": str(xyz_dir),
        "dataset_dir": str(dataset_dir),
        "reaction_count": len(rows),
    }


def _build_rxnresid_bundle(*, export_path: Path, source: Path, root: Path) -> dict[str, object]:
    rows = _read_csv(export_path)
    if not rows:
        raise SystemExit(f"RxnResid export is empty: {export_path}")
    required = ("mapped_reaction_smiles", "activation_gibbs_free_energy_kcal_mol")
    if any(key not in rows[0] for key in required):
        raise SystemExit(f"RxnResid export must contain columns: {', '.join(required)}")

    _copy_source(source, root)
    data_dir = root / "data"
    split_dir = data_dir / "splits"
    data_dir.mkdir(parents=True, exist_ok=True)
    split_dir.mkdir(parents=True, exist_ok=True)

    group_routes: defaultdict[str, int] = defaultdict(int)
    normalized: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        reaction = _require(row, "mapped_reaction_smiles", path=export_path)
        target = _require(row, "activation_gibbs_free_energy_kcal_mol", path=export_path)
        try:
            float(target)
        except ValueError as exc:
            raise SystemExit(f"{export_path}: row {index} has a non-numeric activation energy") from exc
        try:
            reactants, _products = reaction.split(">>", 1)
        except ValueError as exc:
            raise SystemExit(f"{export_path}: row {index} is not a mapped reaction") from exc
        components = [component for component in reactants.split(".") if component]
        if len(components) < 2:
            raise SystemExit(f"{export_path}: row {index} has fewer than two reactant components")
        group_key = _strip_atom_maps(reactants)
        route_id = group_routes[group_key]
        group_routes[group_key] += 1
        if route_id > 7:
            raise SystemExit(
                f"{export_path}: reactant group at row {index} has more than 8 routes; "
                "the upstream route_id_count is 8"
            )
        normalized.append(
            {
                "source_row_index": index,
                "rxn_smiles": reaction,
                "ene_id": _component_id(components[0], "ene"),
                "diene_id": _component_id(components[1], "diene"),
                "prod_id": route_id,
                "G_T_activate": target,
                "reaction_id": row.get("mapped_reaction_id", ""),
            }
        )

    native_csv = data_dir / "demo_db.csv"
    _write_csv(
        native_csv,
        ["source_row_index", "rxn_smiles", "ene_id", "diene_id", "prod_id", "G_T_activate", "reaction_id"],
        normalized,
    )
    config_source = source / "configs" / "rxnresid.yaml"
    config_text = config_source.read_text(encoding="utf-8")
    config_text = config_text.replace(
        "path: data/new_full_df_exact_balanced_40ene_40diene.csv", "path: data/demo_db.csv"
    )
    config_text = config_text.replace("cache_enabled: true", "cache_enabled: false")
    config_text = config_text.replace(
        "split_manifest: data/splits/group_10fold_seed42.json",
        "split_manifest: data/splits/demo.json",
    )
    (root / "configs" / "rxnresid-demo.yaml").write_text(config_text, encoding="utf-8")
    (root / "data" / "SOURCE.txt").write_text(
        f"source_export={export_path}\nsource_repository=https://github.com/gentle1999/RxnResid\n"
        f"source_revision={RXNRESID_REVISION}\n",
        encoding="utf-8",
    )
    return {
        "input_directory": str(root),
        "dataset": str(native_csv),
        "config": str(root / "configs" / "rxnresid-demo.yaml"),
        "row_count": len(normalized),
        "group_count": len(group_routes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rits-export", type=Path, required=True)
    parser.add_argument("--rxnresid-export", type=Path, required=True)
    parser.add_argument("--rits-source", type=Path, required=True)
    parser.add_argument("--rxnresid-source", type=Path, required=True)
    parser.add_argument("--rits-checkpoint", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    output = args.out_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    rits = _build_rits_bundle(
        export_path=args.rits_export.resolve(),
        source=args.rits_source.resolve(),
        checkpoint=args.rits_checkpoint.resolve(),
        root=output / "rits",
    )
    rxnresid = _build_rxnresid_bundle(
        export_path=args.rxnresid_export.resolve(),
        source=args.rxnresid_source.resolve(),
        root=output / "rxnresid",
    )
    print({"rits": rits, "rxnresid": rxnresid})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
