#!/usr/bin/env python3
"""Build deterministic RDKit structure signals for one acquisition round.

The tool analyzes candidate reaction SMILES locally. It does not select
candidates, access a model, or submit a computation. Its feature table and JSON
report are intended to be reviewed and cited by the local acquisition agent.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections.abc import Sequence
from pathlib import Path
from typing import Any


SCHEMA = "tricycle.agent-acquisition-structure-analysis/v1"
DEFAULT_FINGERPRINT_RADIUS = 2
DEFAULT_FINGERPRINT_BITS = 2048
DEFAULT_DENSITY_NEIGHBORS = 10
ID_COLUMNS = ("candidate_id", "reaction_id", "path_id", "pair_id", "id")
REACTION_COLUMNS = (
    "mapped_reaction_smiles",
    "reaction_smiles",
    "rxn_smiles",
    "rxn_smiles_mapped",
)
FEATURE_COLUMNS = (
    "reactant_component_count",
    "product_component_count",
    "reactant_atom_count",
    "product_atom_count",
    "reactant_heavy_atom_count",
    "product_heavy_atom_count",
    "reactant_heteroatom_count",
    "product_heteroatom_count",
    "reactant_aromatic_atom_count",
    "product_aromatic_atom_count",
    "reactant_ring_count",
    "product_ring_count",
    "reactant_rotatable_bond_count",
    "product_rotatable_bond_count",
    "reactant_formal_charge",
    "product_formal_charge",
    "product_chiral_center_count",
    "product_unassigned_chiral_center_count",
    "atom_maps_complete",
    "formed_bond_count",
    "broken_bond_count",
    "changed_bond_count",
    "reaction_edit_count",
    "nearest_labeled_similarity",
    "sampled_novelty",
    "pool_nearest_similarity",
    "pool_local_density",
)


class StructureAnalysisError(ValueError):
    """Raised when an input structure table cannot be analyzed completely."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_ids(values: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            if reader.fieldnames is None or not reader.fieldnames:
                raise StructureAnalysisError(f"input has no CSV header: {path}")
            fields = [str(field) for field in reader.fieldnames]
            if len(fields) != len(set(fields)):
                raise StructureAnalysisError(f"input has duplicate header fields: {path}")
            rows: list[dict[str, str]] = []
            for line_number, row in enumerate(reader, start=2):
                if None in row:
                    raise StructureAnalysisError(
                        f"input row has too many values at {path}:{line_number}"
                    )
                rows.append({key: value or "" for key, value in row.items()})
    except (OSError, UnicodeError, csv.Error) as exc:
        raise StructureAnalysisError(f"cannot read CSV {path}: {exc}") from exc
    return fields, rows


def _resolve_column(
    fields: Sequence[str], requested: str | None, candidates: Sequence[str], *, label: str
) -> str:
    if requested:
        if requested not in fields:
            raise StructureAnalysisError(
                f"{label} column {requested!r} is absent; available columns: {', '.join(fields)}"
            )
        return requested
    for candidate in candidates:
        if candidate in fields:
            return candidate
    raise StructureAnalysisError(
        f"cannot infer {label} column; specify it explicitly (available: {', '.join(fields)})"
    )


def _finite_text(value: float) -> str:
    if not math.isfinite(value):
        raise StructureAnalysisError("non-finite structure feature was produced")
    return format(value, ".12g")


def _parse_reaction(
    reaction_smiles: str,
    *,
    candidate_id: str,
    Chem: Any,
) -> tuple[list[Any], list[Any], str, str]:
    if ">>" in reaction_smiles:
        sides = reaction_smiles.split(">>")
        if len(sides) != 2:
            raise StructureAnalysisError(
                f"candidate {candidate_id!r} has malformed reaction SMILES"
            )
        reactant_text, product_text = sides
    else:
        sides = reaction_smiles.split(">")
        if len(sides) != 3:
            raise StructureAnalysisError(
                f"candidate {candidate_id!r} must use reactants>>products or reactants>agents>products"
            )
        reactant_text, _agent_text, product_text = sides
    if not reactant_text.strip() or not product_text.strip():
        raise StructureAnalysisError(
            f"candidate {candidate_id!r} has an empty reactant or product side"
        )

    def parse_side(text: str, side: str) -> list[Any]:
        molecules: list[Any] = []
        for component in text.split("."):
            if not component.strip():
                raise StructureAnalysisError(
                    f"candidate {candidate_id!r} has an empty {side} component"
                )
            molecule = Chem.MolFromSmiles(component)
            if molecule is None:
                raise StructureAnalysisError(
                    f"candidate {candidate_id!r} has invalid {side} SMILES: {component!r}"
                )
            molecules.append(molecule)
        return molecules

    reactants = parse_side(reactant_text, "reactant")
    products = parse_side(product_text, "product")

    def canonical_side(molecules: Sequence[Any]) -> str:
        canonical: list[str] = []
        for molecule in molecules:
            unmapped = Chem.Mol(molecule)
            for atom in unmapped.GetAtoms():
                atom.SetAtomMapNum(0)
            canonical.append(Chem.MolToSmiles(unmapped, canonical=True, isomericSmiles=True))
        return ".".join(sorted(canonical))

    return reactants, products, canonical_side(reactants), canonical_side(products)


def _side_features(molecules: Sequence[Any], *, rdMolDescriptors: Any) -> dict[str, int]:
    atoms = [atom for molecule in molecules for atom in molecule.GetAtoms()]
    return {
        "component_count": len(molecules),
        "atom_count": sum(molecule.GetNumAtoms() for molecule in molecules),
        "heavy_atom_count": sum(molecule.GetNumHeavyAtoms() for molecule in molecules),
        "heteroatom_count": sum(
            1 for atom in atoms if atom.GetAtomicNum() not in (1, 6)
        ),
        "aromatic_atom_count": sum(1 for atom in atoms if atom.GetIsAromatic()),
        "ring_count": sum(rdMolDescriptors.CalcNumRings(molecule) for molecule in molecules),
        "rotatable_bond_count": sum(
            rdMolDescriptors.CalcNumRotatableBonds(molecule) for molecule in molecules
        ),
        "formal_charge": sum(atom.GetFormalCharge() for atom in atoms),
    }


def _side_fingerprint(
    molecules: Sequence[Any], *, Chem: Any, fingerprint_generator: Any, bit_count: int
) -> Any:
    from rdkit import DataStructs

    combined = DataStructs.ExplicitBitVect(bit_count)
    for molecule in molecules:
        unmapped = Chem.Mol(molecule)
        for atom in unmapped.GetAtoms():
            atom.SetAtomMapNum(0)
        fingerprint = fingerprint_generator.GetFingerprint(unmapped)
        for bit in fingerprint.GetOnBits():
            combined.SetBit(bit)
    return combined


def _mapped_side(
    molecules: Sequence[Any],
) -> tuple[dict[int, int], dict[tuple[int, int], float]] | None:
    atom_maps: set[int] = set()
    atom_numbers: dict[int, int] = {}
    bonds: dict[tuple[int, int], float] = {}
    for molecule in molecules:
        for atom in molecule.GetAtoms():
            map_number = atom.GetAtomMapNum()
            if map_number < 1 or map_number in atom_maps:
                return None
            atom_maps.add(map_number)
            atom_numbers[map_number] = atom.GetAtomicNum()
        for bond in molecule.GetBonds():
            left = molecule.GetAtomWithIdx(bond.GetBeginAtomIdx()).GetAtomMapNum()
            right = molecule.GetAtomWithIdx(bond.GetEndAtomIdx()).GetAtomMapNum()
            if left < 1 or right < 1:
                return None
            key = tuple(sorted((left, right)))
            bonds[key] = float(bond.GetBondTypeAsDouble())
    return atom_numbers, bonds


def _reaction_edits(reactants: Sequence[Any], products: Sequence[Any]) -> dict[str, int | str]:
    reactant_side = _mapped_side(reactants)
    product_side = _mapped_side(products)
    if reactant_side is None or product_side is None:
        return {
            "atom_maps_complete": 0,
            "formed_bond_count": "",
            "broken_bond_count": "",
            "changed_bond_count": "",
            "reaction_edit_count": "",
        }
    reactant_atoms, reactant_bonds = reactant_side
    product_atoms, product_bonds = product_side
    if reactant_atoms != product_atoms:
        return {
            "atom_maps_complete": 0,
            "formed_bond_count": "",
            "broken_bond_count": "",
            "changed_bond_count": "",
            "reaction_edit_count": "",
        }
    keys = set(reactant_bonds) | set(product_bonds)
    formed = sum(key not in reactant_bonds for key in keys)
    broken = sum(key not in product_bonds for key in keys)
    changed = sum(
        key in reactant_bonds
        and key in product_bonds
        and reactant_bonds[key] != product_bonds[key]
        for key in keys
    )
    return {
        "atom_maps_complete": 1,
        "formed_bond_count": formed,
        "broken_bond_count": broken,
        "changed_bond_count": changed,
        "reaction_edit_count": formed + broken + changed,
    }


def _bulk_similarities(
    query: tuple[Any, Any],
    targets: Sequence[tuple[Any, Any]],
    *,
    DataStructs: Any,
) -> list[float]:
    if not targets:
        return []
    reactant_scores = DataStructs.BulkTanimotoSimilarity(
        query[0], [target[0] for target in targets]
    )
    product_scores = DataStructs.BulkTanimotoSimilarity(
        query[1], [target[1] for target in targets]
    )
    return [0.5 * (reactant + product) for reactant, product in zip(
        reactant_scores, product_scores, strict=True
    )]


def _summary(values: Sequence[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": min(ordered),
        "mean": statistics.fmean(ordered),
        "median": statistics.median(ordered),
        "p90": ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)],
        "max": max(ordered),
    }


def analyze(
    *,
    candidates_path: Path,
    labeled_path: Path | None,
    output_dir: Path,
    candidate_id_column: str | None,
    reaction_smiles_column: str | None,
    labeled_id_column: str | None,
    labeled_reaction_smiles_column: str | None,
    fingerprint_radius: int,
    fingerprint_bits: int,
    density_neighbors: int,
) -> dict[str, Any]:
    if fingerprint_radius < 0 or fingerprint_bits < 1 or density_neighbors < 1:
        raise StructureAnalysisError("fingerprint and density parameters must be positive")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise StructureAnalysisError(f"refusing to overwrite non-empty output directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        from rdkit import Chem, DataStructs, rdBase
        from rdkit.Chem import rdFingerprintGenerator, rdMolDescriptors
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise StructureAnalysisError(
            "RDKit is required; run this tool in the tricycle-reactionopt environment"
        ) from exc

    candidate_fields, raw_candidates = _read_csv(candidates_path)
    if not raw_candidates:
        raise StructureAnalysisError(f"candidate table is empty: {candidates_path}")
    id_column = _resolve_column(candidate_fields, candidate_id_column, ID_COLUMNS, label="candidate ID")
    reaction_column = _resolve_column(
        candidate_fields, reaction_smiles_column, REACTION_COLUMNS, label="reaction SMILES"
    )
    if id_column != "candidate_id" and "candidate_id" in candidate_fields:
        mismatches = [
            row
            for row in raw_candidates
            if row.get("candidate_id", "").strip() != row.get(id_column, "").strip()
        ]
        if mismatches:
            raise StructureAnalysisError(
                f"candidate_id disagrees with selected ID column {id_column!r}"
            )
    candidate_rows: list[dict[str, str]] = []
    candidate_ids: list[str] = []
    seen_candidate_ids: set[str] = set()
    candidate_structures: list[tuple[list[Any], list[Any], str, str]] = []
    for index, row in enumerate(raw_candidates, start=2):
        candidate_id = row.get(id_column, "").strip()
        if not candidate_id:
            raise StructureAnalysisError(f"empty candidate ID at {candidates_path}:{index}")
        if candidate_id in seen_candidate_ids:
            raise StructureAnalysisError(f"duplicate candidate ID: {candidate_id!r}")
        structure = _parse_reaction(
            row.get(reaction_column, "").strip(), candidate_id=candidate_id, Chem=Chem
        )
        candidate_ids.append(candidate_id)
        seen_candidate_ids.add(candidate_id)
        candidate_rows.append(row)
        candidate_structures.append(structure)

    labeled_fields: list[str] = []
    raw_labeled: list[dict[str, str]] = []
    labeled_ids: list[str] = []
    seen_labeled_ids: set[str] = set()
    labeled_structures: list[tuple[list[Any], list[Any], str, str]] = []
    if labeled_path is not None:
        labeled_fields, raw_labeled = _read_csv(labeled_path)
        if raw_labeled:
            train_id_column = _resolve_column(
                labeled_fields, labeled_id_column, ID_COLUMNS, label="labeled candidate ID"
            )
            train_reaction_column = _resolve_column(
                labeled_fields,
                labeled_reaction_smiles_column,
                REACTION_COLUMNS,
                label="labeled reaction SMILES",
            )
            for index, row in enumerate(raw_labeled, start=2):
                candidate_id = row.get(train_id_column, "").strip()
                if not candidate_id:
                    raise StructureAnalysisError(f"empty labeled candidate ID at {labeled_path}:{index}")
                if candidate_id in seen_labeled_ids:
                    raise StructureAnalysisError(f"duplicate labeled candidate ID: {candidate_id!r}")
                if candidate_id in candidate_ids:
                    raise StructureAnalysisError(
                        f"candidate {candidate_id!r} occurs in both remaining and labeled tables"
                    )
                structure = _parse_reaction(
                    row.get(train_reaction_column, "").strip(), candidate_id=candidate_id, Chem=Chem
                )
                labeled_ids.append(candidate_id)
                seen_labeled_ids.add(candidate_id)
                labeled_structures.append(structure)

    generator = rdFingerprintGenerator.GetMorganGenerator(
        radius=fingerprint_radius,
        fpSize=fingerprint_bits,
        includeChirality=True,
    )

    def fingerprint_pair(
        structures: Sequence[tuple[list[Any], list[Any], str, str]],
    ) -> list[tuple[Any, Any]]:
        return [
            (
                _side_fingerprint(reactants, Chem=Chem, fingerprint_generator=generator, bit_count=fingerprint_bits),
                _side_fingerprint(products, Chem=Chem, fingerprint_generator=generator, bit_count=fingerprint_bits),
            )
            for reactants, products, _reactant_smiles, _product_smiles in structures
        ]

    candidate_fingerprints = fingerprint_pair(candidate_structures)
    labeled_fingerprints = fingerprint_pair(labeled_structures)
    output_rows: list[dict[str, object]] = []
    for row, candidate_id, structure, fingerprints in zip(
        candidate_rows, candidate_ids, candidate_structures, candidate_fingerprints, strict=True
    ):
        reactants, products, canonical_reactants, canonical_products = structure
        reactant_features = _side_features(reactants, rdMolDescriptors=rdMolDescriptors)
        product_features = _side_features(products, rdMolDescriptors=rdMolDescriptors)
        chiral_centers = [
            center
            for molecule in products
            for center in Chem.FindMolChiralCenters(molecule, includeUnassigned=True)
        ]
        nearest_labeled_similarity: float | None = None
        if labeled_fingerprints:
            labeled_similarities = _bulk_similarities(
                fingerprints, labeled_fingerprints, DataStructs=DataStructs
            )
            nearest_labeled_similarity = max(labeled_similarities)
            nearest_labeled_id = labeled_ids[labeled_similarities.index(nearest_labeled_similarity)]
            sampled_novelty = 1.0 - nearest_labeled_similarity
        else:
            nearest_labeled_id = ""
            sampled_novelty = 1.0

        other_indices = [index for index, other_id in enumerate(candidate_ids) if other_id != candidate_id]
        other_fingerprints = [candidate_fingerprints[index] for index in other_indices]
        other_similarities = _bulk_similarities(
            fingerprints, other_fingerprints, DataStructs=DataStructs
        )
        other_similarities.sort(reverse=True)
        pool_nearest_similarity = other_similarities[0] if other_similarities else 0.0
        local_neighbors = other_similarities[:density_neighbors]
        pool_local_density = statistics.fmean(local_neighbors) if local_neighbors else 0.0

        edits = _reaction_edits(reactants, products)
        features: dict[str, object] = {
            "candidate_id": candidate_id,
            "canonical_reaction_smiles": f"{canonical_reactants}>>{canonical_products}",
            "canonical_reactants": canonical_reactants,
            "canonical_products": canonical_products,
            "nearest_labeled_candidate_id": nearest_labeled_id,
            "labeled_reference_count": len(labeled_fingerprints),
            "nearest_labeled_similarity": (
                "" if nearest_labeled_similarity is None else _finite_text(nearest_labeled_similarity)
            ),
            "sampled_novelty": _finite_text(sampled_novelty),
            "pool_nearest_similarity": _finite_text(pool_nearest_similarity),
            "pool_local_density": _finite_text(pool_local_density),
            "product_chiral_center_count": len(chiral_centers),
            "product_unassigned_chiral_center_count": sum(center[1] == "?" for center in chiral_centers),
            **{f"reactant_{key}": value for key, value in reactant_features.items()},
            **{f"product_{key}": value for key, value in product_features.items()},
            **edits,
        }
        output_rows.append({**row, **features})

    extra_columns = (
        "candidate_id",
        "canonical_reaction_smiles",
        "canonical_reactants",
        "canonical_products",
        "nearest_labeled_candidate_id",
        "labeled_reference_count",
        *FEATURE_COLUMNS,
    )
    overwritten_columns = sorted(set(extra_columns) & set(candidate_fields))
    output_fields = list(dict.fromkeys([*candidate_fields, *extra_columns]))
    output_path = output_dir / "structure-features.csv"
    with output_path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(output_rows)

    feature_summaries = {
        name: _summary(
            [float(str(row[name])) for row in output_rows if str(row.get(name, "")).strip()]
        )
        for name in (
            "sampled_novelty",
            "nearest_labeled_similarity",
            "pool_nearest_similarity",
            "pool_local_density",
            "reactant_heavy_atom_count",
            "product_heavy_atom_count",
            "reactant_ring_count",
            "product_ring_count",
            "reactant_rotatable_bond_count",
            "product_rotatable_bond_count",
            "reaction_edit_count",
        )
    }
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "tool": Path(__file__).name,
        "tool_sha256": _sha256_file(Path(__file__).resolve()),
        "rdkit_version": rdBase.rdkitVersion,
        "inputs": {
            "candidates": {
                "path": str(candidates_path.resolve()),
                "sha256": _sha256_file(candidates_path),
                "row_count": len(candidate_rows),
                "id_column": id_column,
                "reaction_smiles_column": reaction_column,
                "ordered_id_sha256": _hash_ids(candidate_ids),
            },
            "labeled": {
                "path": str(labeled_path.resolve()) if labeled_path else "",
                "sha256": _sha256_file(labeled_path) if labeled_path else "",
                "row_count": len(labeled_ids),
                "id_column": (
                    _resolve_column(labeled_fields, labeled_id_column, ID_COLUMNS, label="labeled candidate ID")
                    if raw_labeled
                    else ""
                ),
                "reaction_smiles_column": (
                    _resolve_column(
                        labeled_fields,
                        labeled_reaction_smiles_column,
                        REACTION_COLUMNS,
                        label="labeled reaction SMILES",
                    )
                    if raw_labeled
                    else ""
                ),
                "ordered_id_sha256": _hash_ids(labeled_ids),
                "empty_reason": "no labeled structure table supplied or table contained no rows"
                if not labeled_ids
                else "",
            },
        },
        "parameters": {
            "fingerprint": "Morgan",
            "fingerprint_radius": fingerprint_radius,
            "fingerprint_bits": fingerprint_bits,
            "include_chirality": True,
            "reaction_similarity": "mean(reactant-side Tanimoto, product-side Tanimoto)",
            "sampled_novelty": "1 - maximum reaction similarity to labeled candidates; 1.0 when none exist",
            "pool_local_density": f"mean of up to {density_neighbors} largest reaction similarities to other remaining candidates",
            "atom_maps": "removed for canonical SMILES and fingerprints; bond edits are reported only when positive unique map IDs and mapped atom elements agree across both reaction sides",
            "canonicalization": "RDKit canonical isomeric SMILES; no tautomer or charge normalization",
        },
        "structure_validation": {
            "all_candidate_reactions_parsed": True,
            "all_labeled_reactions_parsed": True,
            "candidate_count_with_complete_atom_maps": sum(
                row["atom_maps_complete"] == 1 for row in output_rows
            ),
            "candidate_count_without_complete_atom_maps": sum(
                row["atom_maps_complete"] == 0 for row in output_rows
            ),
        },
        "overwritten_input_columns": overwritten_columns,
        "feature_summaries": feature_summaries,
        "outputs": {
            "feature_table": {
                "path": str(output_path.resolve()),
                "sha256": _sha256_file(output_path),
                "row_count": len(output_rows),
            }
        },
    }
    report_path = output_dir / "structure-analysis.json"
    with report_path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    report["outputs"]["analysis_report"] = {
        "path": str(report_path.resolve()),
        "sha256": _sha256_file(report_path),
    }
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--labeled", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--candidate-id-column", default=None)
    parser.add_argument("--reaction-smiles-column", default=None)
    parser.add_argument("--labeled-id-column", default=None)
    parser.add_argument("--labeled-reaction-smiles-column", default=None)
    parser.add_argument("--fingerprint-radius", type=int, default=DEFAULT_FINGERPRINT_RADIUS)
    parser.add_argument("--fingerprint-bits", type=int, default=DEFAULT_FINGERPRINT_BITS)
    parser.add_argument("--density-neighbors", type=int, default=DEFAULT_DENSITY_NEIGHBORS)
    args = parser.parse_args(argv)
    try:
        report = analyze(
            candidates_path=args.candidates.resolve(),
            labeled_path=args.labeled.resolve() if args.labeled else None,
            output_dir=args.output_dir.resolve(),
            candidate_id_column=args.candidate_id_column,
            reaction_smiles_column=args.reaction_smiles_column,
            labeled_id_column=args.labeled_id_column,
            labeled_reaction_smiles_column=args.labeled_reaction_smiles_column,
            fingerprint_radius=args.fingerprint_radius,
            fingerprint_bits=args.fingerprint_bits,
            density_neighbors=args.density_neighbors,
        )
    except (OSError, StructureAnalysisError) as exc:
        parser.error(str(exc))
        return 2
    print(
        json.dumps(
            {
                "row_count": report["outputs"]["feature_table"]["row_count"],
                "feature_table": report["outputs"]["feature_table"]["path"],
                "analysis_report": report["outputs"]["analysis_report"]["path"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
