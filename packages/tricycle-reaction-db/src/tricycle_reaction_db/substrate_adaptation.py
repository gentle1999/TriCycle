"""Append project-scoped substrate identities while preserving every native profile."""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from functools import cache
from pathlib import Path
from typing import Any

from tricycle_core.io import file_sha256 as core_file_sha256
from tricycle_core.io import write_delimited as _write_csv

SUBSTRATE_MATCH_POLICY = "mapping-independent-stereo-graph-v2"

def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return [dict(row) for row in reader]

@cache
def _substrate_graph(smiles: str) -> Any:
    """Parse stereo before removing explicit H; never round-trip for identity."""
    from rdkit import Chem

    params: Any = Chem.SmilesParserParams()
    params.removeHs = False
    molecule = Chem.MolFromSmiles(smiles, params)
    if molecule is None:
        raise ValueError(f"cannot parse substrate SMILES: {smiles!r}")
    for atom in molecule.GetAtoms():
        atom.SetAtomMapNum(0)
    editable = Chem.RWMol(molecule)
    for bond in editable.GetBonds():
        left, right = bond.GetBeginAtom(), bond.GetEndAtom()
        if bond.GetBondType() == Chem.BondType.SINGLE and (
            (left.GetSymbol() == "O" and left.GetFormalCharge() == -1 and right.GetFormalCharge() == 1)
            or (right.GetSymbol() == "O" and right.GetFormalCharge() == -1 and left.GetFormalCharge() == 1)
        ):
            left.SetFormalCharge(0)
            right.SetFormalCharge(0)
            bond.SetBondType(Chem.BondType.DOUBLE)
    normalized = editable.GetMol()
    Chem.SanitizeMol(normalized)
    # Parsing with the default removeHs=True can invert ring-alkene stereo.
    # Broad Cleanup() can also reassign it from traversal-dependent BondDir.
    # Remove ordinary H only after the complete source stereo was parsed.
    return Chem.RemoveHs(normalized)

def _substrate_connectivity_key(molecule: Any) -> str:
    from rdkit import Chem

    # Coarse lookup only: stereo/isotopes are checked on the complete graph.
    return str(Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=False))

def _same_substrate_graph(left: Any, right: Any) -> bool:
    """Exact graph isomorphism with stereo, isotopes, charge, radicals, and H."""
    from rdkit import Chem

    if left.GetNumAtoms() != right.GetNumAtoms() or left.GetNumBonds() != right.GetNumBonds():
        return False
    params: Any = Chem.SubstructMatchParameters()
    params.useChirality = True
    params.useEnhancedStereo = True
    params.maxMatches = 10000
    # An unspecified query can match a specified target. Require the reverse
    # stereo-aware match as well so missing stereo never becomes equivalence.
    if not right.HasSubstructMatch(left, params):
        return False

    def atom_state(atom: Any) -> tuple[int, ...]:
        return (
            atom.GetAtomicNum(), atom.GetIsotope(), atom.GetFormalCharge(),
            atom.GetNumRadicalElectrons(), atom.GetTotalNumHs(includeNeighbors=True),
            int(atom.GetIsAromatic()),
        )

    return any(
        all(atom_state(left.GetAtomWithIdx(target)) == atom_state(right.GetAtomWithIdx(query))
            for query, target in enumerate(match))
        for match in left.GetSubstructMatches(right, params)
    )

def _project_reactant_aliases(
    alias_path: Path, project_id: str | None,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Read original mapped participants from project-scoped database evidence.

    The old canonical-string dictionary is not an identity source: its writer
    could have changed stereo. Reuse its original reactant/artifact bindings.
    """
    proof_path = alias_path.parent / "corrected-summary.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    if not project_id or proof.get("project_id") != project_id:
        raise ValueError("reactant aliases do not belong to the requested database project")
    legacy = json.loads(alias_path.read_text(encoding="utf-8"))
    if set(legacy) != {"ene", "diene"}:
        raise ValueError("source aliases must contain only ene and diene structure indexes")
    paths = sorted((alias_path.parent / "raw").glob("details-*.json"))
    paths += sorted((alias_path.parent / "alias-raw").glob("*.json"))
    hashes = {str(proof_path.resolve()): core_file_sha256(proof_path)}
    aliases: dict[tuple[str, str, str, str], dict[str, str]] = {}
    pattern = re.compile(r"(?P<id>\d+)_\d+\.(?P<role>ene|diene)\.log$", re.I)
    for path in paths:
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("project_id") != project_id:
            raise ValueError("reactant binding evidence belongs to a different project")
        hashes[str(path.resolve())] = core_file_sha256(path)
        service = record.get("data", {}).get("MappedReactionQueryService", {})
        for detail in service.values():
            participants = {row["id"]: row for row in detail.get("participants", [])}
            for node in detail.get("nodes", []):
                if node["role"].lower().replace("-", "_") != "reactant":
                    continue
                for geometry in node.get("geometries", []):
                    participant = participants.get(geometry.get("mapped_reaction_participant_id"))
                    if participant is None:
                        continue
                    for calculation in geometry.get("calculations", []):
                        match = pattern.search(calculation.get("original_filename") or "")
                        if match is None:
                            continue
                        role, ident = match["role"].lower(), str(int(match["id"])).zfill(12)
                        smiles = participant["mapped_smiles"]
                        artifact = calculation["artifact_file_id"]
                        aliases[role, ident, smiles, artifact] = {
                            "role": role, "source_id": ident, "mapped_smiles": smiles,
                            "artifact_id": artifact, "reaction_id": detail["id"],
                        }
    if any(legacy.values()) and not aliases:
        raise ValueError("legacy string aliases require original mapped reactant evidence")
    return list(aliases.values()), hashes

def adapt_database_export(
    export_path: Path,
    substrate_path: Path,
    output_dir: Path,
    *,
    source_aliases: Path | None = None,
    project_id: str | None = None,
) -> dict[str, object]:
    """Append substrate identities to native CSV rows, preserving profile grain.

    Matching uses full stereo graphs and the structure-only input registry.
    Optional aliases come from original mapped reactant evidence in this same
    database project, never old normalized strings, labels, or TS-origin guesses.
    """
    rows = _read_csv(export_path)
    with export_path.open(encoding="utf-8", newline="") as handle:
        columns = list(csv.DictReader(handle).fieldnames or [])
    if not {"mapped_reaction_id", "mapped_reaction_smiles"} <= set(columns):
        raise ValueError("native export is missing reaction identity columns")
    structures: dict[str, dict[str, str]] = {"ene": {}, "diene": {}}
    index: dict[str, defaultdict[str, list[tuple[str, Any]]]] = {
        "ene": defaultdict(list), "diene": defaultdict(list)
    }
    for row in _read_csv(substrate_path):
        for role in structures:
            raw = row[f"{role}_id"].strip().split(".")[0]
            ident = str(int(raw)).zfill(12)
            smiles = row[f"{role}_smiles"]
            previous = structures[role].setdefault(ident, smiles)
            if not _same_substrate_graph(_substrate_graph(previous), _substrate_graph(smiles)):
                raise ValueError(f"inconsistent structure for {role} {ident}")
    for role in structures:
        for ident, smiles in structures[role].items():
            molecule = _substrate_graph(smiles)
            index[role][_substrate_connectivity_key(molecule)].append((ident, molecule))
    alias_index: dict[str, defaultdict[str, list[tuple[str, Any]]]] = {
        "ene": defaultdict(list), "diene": defaultdict(list)
    }
    alias_evidence: dict[str, str] = {}
    if source_aliases is not None:
        aliases, alias_evidence = _project_reactant_aliases(source_aliases, project_id)
        seen_aliases: set[tuple[str, str, str]] = set()
        for alias in aliases:
            role, ident, smiles = alias["role"], alias["source_id"], alias["mapped_smiles"]
            if (role, ident, smiles) in seen_aliases:
                continue
            seen_aliases.add((role, ident, smiles))
            molecule = _substrate_graph(smiles)
            key = _substrate_connectivity_key(molecule)
            if not any(existing == ident and _same_substrate_graph(molecule, target)
                       for existing, target in alias_index[role][key]):
                alias_index[role][key].append((ident, molecule))

    @cache
    def component_ids(smiles: str, role: str, use_aliases: bool) -> frozenset[str]:
        molecule = _substrate_graph(smiles)
        key = _substrate_connectivity_key(molecule)
        direct = frozenset(
            ident for ident, target in index[role].get(key, [])
            if _same_substrate_graph(molecule, target)
        )
        if direct or not use_aliases:
            return direct
        return frozenset(
            ident for ident, target in alias_index[role].get(key, [])
            if _same_substrate_graph(molecule, target)
        )

    additions = [
        "export_row_index", "pair_id", "ene_id", "ene_smiles", "diene_id", "diene_smiles",
        "substrate_match_status", "substrate_pair_candidates", "reactant_connectivity_keys",
        "substrate_match_method", "substrate_match_policy",
        "activation_free_energy_kcal_mol",
    ]
    if set(columns) & set(additions):
        raise ValueError("native export already contains adaptation columns")
    adapted: list[dict[str, object]] = []
    statuses: defaultdict[str, int] = defaultdict(int)
    for ordinal, source in enumerate(rows):
        components: list[str] = []
        choices: set[tuple[str, str]] = set()
        status = "unmatched"
        method = "no-unique-graph-match"
        try:
            sides = source["mapped_reaction_smiles"].split(">>")
            if len(sides) != 2:
                raise ValueError("reaction must have exactly two sides")
            raw_components = sides[0].split(".")
            components = [_substrate_connectivity_key(_substrate_graph(part)) for part in raw_components]
            if len(components) == 2:
                for use_aliases in (False, True):
                    for ene_pos, diene_pos in ((0, 1), (1, 0)):
                        choices.update(
                            (ene, diene)
                            for ene in component_ids(raw_components[ene_pos], "ene", use_aliases)
                            for diene in component_ids(raw_components[diene_pos], "diene", use_aliases)
                        )
                    if choices:
                        method = "project-reactant-graph-alias" if use_aliases else "registry-stereo-graph"
                        break
            else:
                status = "unsupported_component_count"
        except ValueError:
            status = "invalid_reaction_smiles"
        pair: tuple[str, str] | None = None
        if len(choices) == 1:
            proposed = next(iter(choices))
            if all(ident in structures[role] for role, ident in zip(structures, proposed, strict=True)):
                pair, status = proposed, "matched"
            else:
                status = "outside_registry"
        elif len(choices) > 1:
            status = "ambiguous"
        statuses[status] += 1
        adapted.append({
            **source, "export_row_index": ordinal,
            "pair_id": "+".join(pair) if pair else "",
            "ene_id": pair[0] if pair else "",
            "ene_smiles": structures["ene"][pair[0]] if pair else "",
            "diene_id": pair[1] if pair else "",
            "diene_smiles": structures["diene"][pair[1]] if pair else "",
            "substrate_match_status": status,
            "substrate_pair_candidates": json.dumps(sorted(choices)),
            "reactant_connectivity_keys": json.dumps(components),
            "substrate_match_method": method,
            "substrate_match_policy": SUBSTRATE_MATCH_POLICY,
            "activation_free_energy_kcal_mol": source.get("activation_gibbs_free_energy_kcal_mol", ""),
        })
    adapted.sort(key=lambda row: (
        row["substrate_match_status"] != "matched", row["ene_id"], row["diene_id"],
        int(str(row["export_row_index"])),
    ))
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "reaction-collection.csv"
    _write_csv(destination, columns + additions, adapted)
    _write_csv(
        output_dir / "unmatched.csv", columns + additions,
        [row for row in adapted if row["substrate_match_status"] != "matched"],
    )
    # Verify preservation against the saved output, not only in-memory rows.
    written = _read_csv(destination)
    if len(written) != len(rows) or any(
        {column: row[column] for column in columns} != rows[int(row["export_row_index"])]
        for row in written
    ):
        raise ValueError("adaptation changed a database-exported value or row count")
    pairs = {(row["ene_id"], row["diene_id"]) for row in adapted if row["pair_id"]}
    labeled = [row for row in adapted if str(row["activation_free_energy_kcal_mol"]).strip()]
    receipt: dict[str, object] = {
        "source": str(export_path.resolve()),
        "source_sha256": core_file_sha256(export_path),
        "substrate_registry": str(substrate_path.resolve()),
        "substrate_registry_sha256": core_file_sha256(substrate_path),
        "source_aliases": str(source_aliases.resolve()) if source_aliases else None,
        "source_aliases_sha256": core_file_sha256(source_aliases) if source_aliases else None,
        "alias_binding_evidence_sha256": alias_evidence,
        "substrate_match_policy": SUBSTRATE_MATCH_POLICY,
        "connectivity_keys_are_identity": False,
        "coverage_interpretation": "matched substrate pairs only; not proof of database completeness",
        "row_count": len(rows), "mapped_reaction_count": len({row["mapped_reaction_id"] for row in rows}),
        "matched_pair_count": len(pairs), "match_status_counts": dict(statuses),
        "activation_labeled_rows": len(labeled),
        "activation_labeled_matched_rows": sum(row["substrate_match_status"] == "matched" for row in labeled),
        "activation_labeled_pair_count": len({row["pair_id"] for row in labeled if row["pair_id"]}),
        "profile_policy": "preserve every native export row; no label selection or aggregation",
        "project_id": project_id,
        "all_source_values_preserved": True,
        "destination": str(destination.resolve()),
        "sha256": core_file_sha256(destination),
    }
    from rdkit import rdBase

    receipt["rdkit_version"] = rdBase.rdkitVersion
    (output_dir / "adaptation.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return receipt
