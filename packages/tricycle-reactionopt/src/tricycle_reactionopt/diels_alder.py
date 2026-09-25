"""Enumerate atom-mapped Diels–Alder reaction candidates from substrate SMILES."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from tricycle_core.errors import InvalidRequestError

from .models import DielsAlderEnumerationRequest, DielsAlderSubstratePair

DA_REACTION_SMARTS = (
    "[#6+0,#7+0:1]=[#6+0,#7+0:2]."
    "[#6+0,#7+0:3]=[#6+0,#7+0:4]-[#6+0,#7+0:5]=[#6+0,#7+0:6]"
    ">>"
    "[#6+0,#7+0:1]1-[#6+0,#7+0:2]-[#6+0,#7+0:3]-"
    "[#6+0,#7+0:4]=[#6+0,#7+0:5]-[#6+0,#7+0:6]1"
)

_CANDIDATE_FIELDS = (
    "reaction_id",
    "pair_id",
    "ene_id",
    "diene_id",
    "ene_smiles",
    "diene_smiles",
    "product_id",
    "product_smiles",
    "mapped_reaction_smiles",
    "stereochemistry",
    "enumeration_complete",
)
_SOURCE_MAP_PROPERTY = "_tricycle_source_atom_map"


@dataclass(frozen=True, slots=True)
class DielsAlderEnumeration:
    candidates: tuple[dict[str, Any], ...]
    substrate_pair_count: int
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_count": len(self.candidates),
            "substrate_pair_count": self.substrate_pair_count,
            "complete": self.complete,
            "candidates": [dict(candidate) for candidate in self.candidates],
        }


def _chem() -> Any:
    try:
        from rdkit import Chem
    except ImportError as exc:  # pragma: no cover - installation dependent
        raise InvalidRequestError(
            "Diels–Alder enumeration requires RDKit; install tricycle-reactionopt"
        ) from exc
    return Chem


@lru_cache(maxsize=1)
def _reaction() -> Any:
    from rdkit.Chem import rdChemReactions

    reaction = rdChemReactions.ReactionFromSmarts(DA_REACTION_SMARTS)
    if reaction is None:
        raise RuntimeError("could not compile the built-in Diels–Alder reaction SMARTS")
    reaction.Initialize()
    return reaction


@lru_cache(maxsize=1)
def _stereo_queries() -> dict[str, tuple[Any, ...]]:
    Chem = _chem()
    patterns = {
        "ene_same": (
            r"[*]/-[#6+0,#7+0]=[#6+0,#7+0]\-[*]",
            r"[*]1[#6+0,#7+0]=[#6+0,#7+0]1",
            r"[*]@[#6+0,#7+0]@=[#6+0,#7+0]@[*]",
        ),
        "ene_diff": (r"[*]/-[#6+0,#7+0]=[#6+0,#7+0]/-[*]",),
        "diene_same": (
            r"[*]/[#6+0,#7+0]=[#6+0,#7+0]\-[#6+0,#7+0]=[#6+0,#7+0]/[*]",
            r"[*]/[#6+0,#7+0]@=[#6+0,#7+0]@\-[#6+0,#7+0]=[#6+0,#7+0]/[*]",
            r"[*]/[#6+0,#7+0]=[#6+0,#7+0]/-[#6+0,#7+0]=[#6+0,#7+0]/[*]",
            r"[*]/[#6+0,#7+0]@=[#6+0,#7+0]@/-[#6+0,#7+0]@=[#6+0,#7+0]/[*]",
        ),
        "diene_diff": (
            r"[*]/[#6+0,#7+0]=[#6+0,#7+0]/-[#6+0,#7+0]=[#6+0,#7+0]\[*]",
        ),
    }
    compiled: dict[str, tuple[Any, ...]] = {}
    for name, smarts_values in patterns.items():
        queries = tuple(Chem.MolFromSmarts(smarts) for smarts in smarts_values)
        if any(query is None for query in queries):
            raise RuntimeError(f"could not compile Diels–Alder stereo query: {name}")
        compiled[name] = queries
    return compiled


@lru_cache(maxsize=1)
def _flippable_atom_queries() -> tuple[Any, ...]:
    """Centers treated as rapidly inverting and left unmarked in products."""

    Chem = _chem()
    patterns = (
        r"[#8,#16,#34;X3;v3;+1]",
        r"[#7,#15,#33;X3;v3;+0]",
        r"[#9,#17,#35,#53;X3;v3;+2]",
    )
    queries = tuple(Chem.MolFromSmarts(pattern) for pattern in patterns)
    if any(query is None for query in queries):
        raise RuntimeError("could not compile flippable-atom SMARTS")
    return queries


def _flippable_atom_indices(molecule: Any) -> tuple[int, ...]:
    return tuple(
        sorted(
            {
                match[0]
                for query in _flippable_atom_queries()
                for match in molecule.GetSubstructMatches(query, uniquify=True)
            }
        )
    )


def _freeze_flippable_stereo_for_enumeration(
    molecule: Any, atom_indices: tuple[int, ...]
) -> None:
    """Keep labile centers out of RDKit's unspecified-stereo enumeration."""

    Chem = _chem()
    for atom_index in atom_indices:
        atom = molecule.GetAtomWithIdx(atom_index)
        if atom.GetChiralTag() == Chem.ChiralType.CHI_UNSPECIFIED:
            atom.SetChiralTag(Chem.ChiralType.CHI_TETRAHEDRAL_CW)


def _clear_flippable_stereo(molecule: Any, atom_indices: tuple[int, ...]) -> None:
    Chem = _chem()
    for atom_index in atom_indices:
        atom = molecule.GetAtomWithIdx(atom_index)
        atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)


def _parse_substrate(smiles: str, label: str, map_offset: int) -> Any:
    Chem = _chem()
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        raise InvalidRequestError(f"could not parse {label}_smiles")
    molecule = Chem.AddHs(molecule)
    for atom in molecule.GetAtoms():
        # Keep the input molecules unmapped during reaction matching. RDKit
        # uses atom maps while ordering symmetric reaction matches, which can
        # otherwise change the legacy stereochemical enumeration behavior.
        atom.SetIntProp(_SOURCE_MAP_PROPERTY, map_offset + atom.GetIdx() + 1)
    return molecule


def _set_ring_stereo(molecule: Any) -> Any:
    """Encode the cis geometry enforced by each ring alkene before reaction."""

    Chem = _chem()
    Chem.Kekulize(molecule)
    for atom_ring in molecule.GetRingInfo().AtomRings():
        ring_size = len(atom_ring)
        for index, bond_begin in enumerate(atom_ring):
            bond_end = atom_ring[(index + 1) % ring_size]
            bond = molecule.GetBondBetweenAtoms(bond_begin, bond_end)
            if (
                bond.GetBondType() != Chem.BondType.DOUBLE
                or not bond.IsInRing()
                or bond.GetStereo() != Chem.BondStereo.STEREONONE
            ):
                continue
            if bond.GetBeginAtomIdx() == bond_end:
                begin_neighbor = atom_ring[(index + 2) % ring_size]
                end_neighbor = atom_ring[index - 1]
            else:
                begin_neighbor = atom_ring[index - 1]
                end_neighbor = atom_ring[(index + 2) % ring_size]
            bond.SetStereoAtoms(begin_neighbor, end_neighbor)
            bond.SetStereo(Chem.BondStereo.STEREOCIS)
    return molecule


def _smiles(molecule: Any, *, mapped: bool) -> str:
    Chem = _chem()
    working = Chem.Mol(molecule)
    if mapped:
        for atom in working.GetAtoms():
            if atom.HasProp(_SOURCE_MAP_PROPERTY):
                atom.SetAtomMapNum(atom.GetIntProp(_SOURCE_MAP_PROPERTY))
        map_numbers = [atom.GetAtomMapNum() for atom in working.GetAtoms()]
        if any(number < 1 for number in map_numbers) or len(set(map_numbers)) != len(
            map_numbers
        ):
            raise InvalidRequestError(
                "mapped reaction SMILES must assign one unique map number to every atom"
            )
    else:
        for atom in working.GetAtoms():
            atom.SetAtomMapNum(0)
    return Chem.MolToSmiles(working, canonical=True, isomericSmiles=True)


def _source_ref(atom: Any) -> tuple[int, int]:
    try:
        return atom.GetIntProp("react_idx"), atom.GetIntProp("react_atom_idx")
    except Exception as exc:
        raise InvalidRequestError(
            "RDKit reaction output did not preserve source atom indices"
        ) from exc


def _mapped_product(
    product: Any,
    reactants: tuple[Any, Any],
) -> tuple[Any, dict[tuple[int, int], int]]:
    source_to_product: dict[tuple[int, int], int] = {}
    for atom in product.GetAtoms():
        source = _source_ref(atom)
        reactant_index, source_atom_index = source
        if reactant_index not in (0, 1):
            raise InvalidRequestError("reaction output contains an unexpected reactant index")
        try:
            source_atom = reactants[reactant_index].GetAtomWithIdx(source_atom_index)
        except Exception as exc:
            raise InvalidRequestError(
                "reaction output contains an invalid source atom index"
            ) from exc
        if source in source_to_product:
            raise InvalidRequestError("reaction output duplicated a source atom")
        source_to_product[source] = atom.GetIdx()
        atom.SetAtomMapNum(0)
        atom.SetIntProp(
            _SOURCE_MAP_PROPERTY,
            source_atom.GetIntProp(_SOURCE_MAP_PROPERTY),
        )

    expected_atoms = sum(molecule.GetNumAtoms() for molecule in reactants)
    if product.GetNumAtoms() != expected_atoms or len(source_to_product) != expected_atoms:
        raise InvalidRequestError(
            "Diels–Alder reaction output did not preserve every explicitly mapped atom"
        )
    expected_maps = [
        atom.GetIntProp(_SOURCE_MAP_PROPERTY)
        for molecule in reactants
        for atom in molecule.GetAtoms()
    ]
    product_maps = [
        atom.GetIntProp(_SOURCE_MAP_PROPERTY) for atom in product.GetAtoms()
    ]
    if (
        len(expected_maps) != len(set(expected_maps))
        or sorted(product_maps) != sorted(expected_maps)
    ):
        raise InvalidRequestError(
            "Diels–Alder product did not preserve the unique substrate atom maps"
        )
    return product, source_to_product


def _cross_bonds(
    product: Any,
    source_to_product: dict[tuple[int, int], int],
) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    for bond in product.GetBonds():
        begin_ref = _source_ref(product.GetAtomWithIdx(bond.GetBeginAtomIdx()))
        end_ref = _source_ref(product.GetAtomWithIdx(bond.GetEndAtomIdx()))
        if begin_ref[0] == end_ref[0]:
            continue
        if {begin_ref[0], end_ref[0]} != {0, 1}:
            continue
        left, right = (begin_ref, end_ref) if begin_ref[0] == 0 else (end_ref, begin_ref)
        if source_to_product.get(left) is None or source_to_product.get(right) is None:
            continue
        result.append((left[1], right[1]))
    if len(result) != 2:
        raise InvalidRequestError(
            "Diels–Alder product must contain exactly two ene–diene forming bonds"
        )
    return tuple(result)


def _diene_paths(
    product: Any,
    reactants: tuple[Any, Any],
    first: int,
    last: int,
) -> tuple[tuple[int, int, int, int], ...]:
    """Find product paths matching the retained six-atom cycloaddition ring."""

    Chem = _chem()
    first_product = next(
        atom.GetIdx()
        for atom in product.GetAtoms()
        if _source_ref(atom) == (1, first)
    )
    last_product = next(
        atom.GetIdx()
        for atom in product.GetAtoms()
        if _source_ref(atom) == (1, last)
    )
    paths: list[tuple[int, int, int, int]] = []

    def visit(current: int, path: tuple[int, ...]) -> None:
        if len(path) == 4:
            if current == last_product:
                source_path = tuple(_source_ref(product.GetAtomWithIdx(index))[1] for index in path)
                source_bonds = [
                    reactants[1].GetBondBetweenAtoms(left, right)
                    for left, right in zip(source_path[:-1], source_path[1:], strict=True)
                ]
                product_bonds = [
                    product.GetBondBetweenAtoms(left, right)
                    for left, right in zip(path[:-1], path[1:], strict=True)
                ]
                if (
                    all(bond is not None for bond in source_bonds)
                    and all(
                        bond.GetBondType() == Chem.BondType.DOUBLE
                        for bond in (source_bonds[0], source_bonds[2])
                    )
                    and source_bonds[1].GetBondType() == Chem.BondType.SINGLE
                    and product_bonds[0].GetBondType() == Chem.BondType.SINGLE
                    and product_bonds[1].GetBondType() == Chem.BondType.DOUBLE
                    and product_bonds[2].GetBondType() == Chem.BondType.SINGLE
                ):
                    paths.append(path)  # type: ignore[arg-type]
            return
        atom = product.GetAtomWithIdx(current)
        for neighbor in atom.GetNeighbors():
            neighbor_index = neighbor.GetIdx()
            if neighbor_index in path or _source_ref(neighbor)[0] != 1:
                continue
            visit(neighbor_index, (*path, neighbor_index))

    visit(first_product, (first_product,))
    return tuple(paths)


def _site_rings(
    product: Any,
    reactants: tuple[Any, Any],
    source_to_product: dict[tuple[int, int], int],
) -> tuple[tuple[int, ...], ...]:
    cross = _cross_bonds(product, source_to_product)
    ene_atoms = sorted({ene for ene, _diene in cross})
    diene_atoms = sorted({diene for _ene, diene in cross})
    if len(ene_atoms) != 2 or len(diene_atoms) != 2:
        raise InvalidRequestError("Diels–Alder reaction site is not a 2+4 atom cycloaddition")
    ene_bond = reactants[0].GetBondBetweenAtoms(*ene_atoms)
    if ene_bond is None or ene_bond.GetBondType() != _chem().BondType.DOUBLE:
        raise InvalidRequestError("formed bonds do not identify an ene double bond")

    diene_for_ene = dict(cross)
    ene_first, ene_second = ene_atoms
    diene_at_first = diene_for_ene[ene_first]
    diene_at_second = diene_for_ene[ene_second]
    rings: list[tuple[int, ...]] = []
    for path in _diene_paths(product, reactants, diene_at_first, diene_at_second):
        # The ring traverses ene_first -> ene_second -> diene_at_second -> ... -> diene_at_first.
        path_product_indices = tuple(reversed(path[1:-1]))
        ring = (
            source_to_product[(0, ene_first)],
            source_to_product[(0, ene_second)],
            source_to_product[(1, diene_at_second)],
            *path_product_indices,
            source_to_product[(1, diene_at_first)],
        )
        if len(ring) == 6 and len(set(ring)) == 6:
            rings.append(ring)
    if not rings:
        raise InvalidRequestError("could not resolve the mapped Diels–Alder product ring")
    return tuple(dict.fromkeys(rings))


def _relation_matches(
    product: Any,
    source_to_product: dict[tuple[int, int], int],
    reactant_index: int,
    atom_a: int,
    neighbor_a: int,
    atom_b: int,
    neighbor_b: int,
    relation: str,
) -> bool:
    """Check a transferred same/different-side constraint using wedge bonds."""

    first = product.GetBondBetweenAtoms(
        source_to_product[(reactant_index, atom_a)],
        source_to_product[(reactant_index, neighbor_a)],
    )
    second = product.GetBondBetweenAtoms(
        source_to_product[(reactant_index, atom_b)],
        source_to_product[(reactant_index, neighbor_b)],
    )
    if first is None or second is None:
        raise InvalidRequestError("product is missing a substrate stereochemistry bond")
    marked = (_chem().BondDir.BEGINDASH, _chem().BondDir.BEGINWEDGE)
    first_dir, second_dir = first.GetBondDir(), second.GetBondDir()
    if first_dir not in marked or second_dir not in marked:
        return True
    if relation == "same":
        return first_dir == second_dir
    return first_dir != second_dir


def _ene_stereo_relations(molecule: Any) -> tuple[tuple[int, int, int, int, str], ...]:
    queries = _stereo_queries()
    relations: list[tuple[int, int, int, int, str]] = []
    for query in queries["ene_same"]:
        for match in molecule.GetSubstructMatches(query, uniquify=True, useChirality=True):
            if len(match) == 4:
                relations.append((match[1], match[0], match[2], match[3], "same"))
            elif len(match) == 3:
                # A three-membered cyclic ene shares its external ring atom.
                relations.append((match[1], match[0], match[2], match[0], "same"))
    for query in queries["ene_diff"]:
        for match in molecule.GetSubstructMatches(query, uniquify=True, useChirality=True):
            relations.append((match[1], match[0], match[2], match[3], "different"))
    return tuple(dict.fromkeys(relations))


def _diene_stereo_relations(molecule: Any) -> tuple[tuple[tuple[int, ...], int, int, str], ...]:
    queries = _stereo_queries()
    relations: list[tuple[tuple[int, ...], int, int, str]] = []
    for query in queries["diene_same"]:
        for match in molecule.GetSubstructMatches(query, uniquify=True, useChirality=True):
            relations.append((tuple(match[1:5]), match[0], match[5], "same"))
    for query in queries["diene_diff"]:
        for match in molecule.GetSubstructMatches(query, uniquify=True, useChirality=True):
            relations.append((tuple(match[1:5]), match[0], match[5], "different"))
    return tuple(dict.fromkeys(relations))


def _clear_reactive_bond_directions(
    product: Any,
    reactants: tuple[Any, Any],
    source_to_product: dict[tuple[int, int], int],
    ring: tuple[int, ...],
) -> None:
    Chem = _chem()
    ring_refs = tuple(_source_ref(product.GetAtomWithIdx(index)) for index in ring)
    reactive_bonds = (
        (ring_refs[0], ring_refs[1]),
        (ring_refs[2], ring_refs[3]),
        (ring_refs[4], ring_refs[5]),
    )
    for (reactant_index, first), (other_reactant_index, second) in reactive_bonds:
        if reactant_index != other_reactant_index:
            continue
        substrate = reactants[reactant_index]
        for center, partner in ((first, second), (second, first)):
            source_center = substrate.GetAtomWithIdx(center)
            for neighbor in source_center.GetNeighbors():
                other = neighbor.GetIdx()
                if other == partner:
                    continue
                source_bond = substrate.GetBondBetweenAtoms(center, other)
                if source_bond.GetBondDir() not in (
                    Chem.BondDir.ENDUPRIGHT,
                    Chem.BondDir.ENDDOWNRIGHT,
                ):
                    continue
                left = source_to_product.get((reactant_index, center))
                right = source_to_product.get((reactant_index, other))
                if left is None or right is None:
                    continue
                product_bond = product.GetBondBetweenAtoms(left, right)
                if product_bond is not None:
                    product_bond.SetBondDir(Chem.BondDir.NONE)


def _stereo_constraints_hold(
    product: Any,
    source_to_product: dict[tuple[int, int], int],
    ene_relations: tuple[tuple[int, int, int, int, str], ...],
    diene_relations: tuple[tuple[tuple[int, ...], int, int, str], ...],
) -> bool:
    Chem = _chem()
    from rdkit.Chem.rdDepictor import Compute2DCoords

    # The legacy generator used RDKit's wedge assignment as its stereochemical
    # reference. Keep that behavior while resolving every queried atom through
    # its reactant source index instead of relying on fixed product indices.
    depicted = Chem.Mol(product)
    for atom in depicted.GetAtoms():
        atom.SetAtomMapNum(0)
    Compute2DCoords(depicted)
    Chem.WedgeMolBonds(depicted, depicted.GetConformer())

    for center_a, substituent_a, center_b, substituent_b, relation in ene_relations:
        if not _relation_matches(
            depicted,
            source_to_product,
            0,
            center_a,
            substituent_a,
            center_b,
            substituent_b,
            relation,
        ):
            return False

    for centers, substituent_a, substituent_b, relation in diene_relations:
        if not _relation_matches(
            depicted,
            source_to_product,
            1,
            centers[0],
            substituent_a,
            centers[-1],
            substituent_b,
            relation,
        ):
            return False

    cross_bonds = _cross_bonds(depicted, source_to_product)
    formed_bond_directions = tuple(
        depicted.GetBondBetweenAtoms(
            source_to_product[(0, ene_atom)],
            source_to_product[(1, diene_atom)],
        ).GetBondDir()
        for ene_atom, diene_atom in cross_bonds
    )
    return not (
        all(direction != Chem.BondDir.NONE for direction in formed_bond_directions)
        and formed_bond_directions[0] != formed_bond_directions[1]
    )


def _candidate_id(pair_id: str, mapped_reaction_smiles: str) -> str:
    digest = hashlib.sha256(
        f"{pair_id}\0{mapped_reaction_smiles}".encode()
    ).hexdigest()[:16]
    return f"da-{digest}"


def _enumerate_pair(
    pair: DielsAlderSubstratePair,
    request: DielsAlderEnumerationRequest,
) -> tuple[list[dict[str, Any]], bool]:
    Chem = _chem()
    from rdkit.Chem.EnumerateStereoisomers import (
        EnumerateStereoisomers,
        StereoEnumerationOptions,
    )

    ene = _parse_substrate(pair.ene_smiles, "ene", 0)
    diene = _parse_substrate(pair.diene_smiles, "diene", ene.GetNumAtoms())
    _set_ring_stereo(ene)
    _set_ring_stereo(diene)
    reactants = (ene, diene)
    product_limit = request.max_products_per_pair
    raw_products = _reaction().RunReactants(
        reactants,
        maxProducts=product_limit + 1 if product_limit is not None else 0,
    )
    if not raw_products:
        raise InvalidRequestError(
            f"substrate pair {pair.pair_id} has no supported neutral C/N ene+diene match"
        )

    product_truncated = False
    if product_limit is not None and len(raw_products) > product_limit:
        raw_products = raw_products[:product_limit]
        product_truncated = True

    ene_relations = (
        _ene_stereo_relations(ene)
        if request.stereochemistry == "reactant-constrained"
        else ()
    )
    diene_relations = (
        _diene_stereo_relations(diene)
        if request.stereochemistry == "reactant-constrained"
        else ()
    )
    products: dict[str, tuple[str, Any]] = {}
    stereo_truncated = False
    for product_tuple in raw_products:
        product, source_to_product = _mapped_product(product_tuple[0], reactants)
        try:
            Chem.SanitizeMol(product)
        except Exception as exc:
            raise InvalidRequestError(
                f"RDKit could not sanitize a product for substrate pair {pair.pair_id}: {exc}"
            ) from exc
        rings = _site_rings(product, reactants, source_to_product)
        for ring in rings:
            _clear_reactive_bond_directions(product, reactants, source_to_product, ring)
        Chem.AssignStereochemistry(product, cleanIt=True, force=True)
        flippable_atom_indices = _flippable_atom_indices(product)
        _freeze_flippable_stereo_for_enumeration(product, flippable_atom_indices)
        options = StereoEnumerationOptions(
            tryEmbedding=False,
            onlyUnassigned=True,
            unique=True,
            maxIsomers=0,
        )
        stereoisomers = iter(EnumerateStereoisomers(product, options=options))
        yielded = 0
        while True:
            isomer = next(stereoisomers, None)
            if isomer is None:
                break
            if (
                request.max_isomers_per_product is not None
                and yielded >= request.max_isomers_per_product
            ):
                stereo_truncated = True
                break
            yielded += 1
            _clear_flippable_stereo(isomer, flippable_atom_indices)
            try:
                Chem.SanitizeMol(isomer)
            except Exception as exc:
                raise InvalidRequestError(
                    "RDKit could not sanitize an enumerated product for substrate pair "
                    f"{pair.pair_id}: {exc}"
                ) from exc
            Chem.AssignStereochemistry(isomer, cleanIt=True, force=True)
            if (
                request.stereochemistry == "reactant-constrained"
                and not _stereo_constraints_hold(
                    isomer,
                    source_to_product,
                    ene_relations,
                    diene_relations,
                )
            ):
                continue
            product_smiles = _smiles(isomer, mapped=False)
            products.setdefault(product_smiles, (_smiles(isomer, mapped=True), isomer))

    complete = not product_truncated and not stereo_truncated
    if not products and complete:
        raise InvalidRequestError(
            "stereochemistry constraints eliminated every product for substrate pair "
            f"{pair.pair_id}"
        )
    mapped_reactants = f"{_smiles(ene, mapped=True)}.{_smiles(diene, mapped=True)}"
    rows: list[dict[str, Any]] = []
    for product_id, product_smiles in enumerate(sorted(products)):
        mapped_product, _product = products[product_smiles]
        mapped_reaction_smiles = f"{mapped_reactants}>>{mapped_product}"
        rows.append(
            {
                "reaction_id": _candidate_id(pair.pair_id, mapped_reaction_smiles),
                "pair_id": pair.pair_id,
                "ene_id": str(pair.ene_id),
                "diene_id": str(pair.diene_id),
                "ene_smiles": pair.ene_smiles,
                "diene_smiles": pair.diene_smiles,
                "product_id": product_id,
                "product_smiles": product_smiles,
                "mapped_reaction_smiles": mapped_reaction_smiles,
                "stereochemistry": request.stereochemistry,
                "enumeration_complete": complete,
            }
        )
    return rows, complete


def enumerate_diels_alder_reactions(
    request: DielsAlderEnumerationRequest,
) -> DielsAlderEnumeration:
    candidates: list[dict[str, Any]] = []
    complete = True
    for pair in request.pairs:
        pair_candidates, pair_complete = _enumerate_pair(pair, request)
        candidates.extend(pair_candidates)
        complete = complete and pair_complete
    return DielsAlderEnumeration(tuple(candidates), len(request.pairs), complete)


def write_mapped_reaction_csv(
    candidates: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    destination: Path,
) -> Path:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=_CANDIDATE_FIELDS, lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(candidates)
    except FileExistsError as exc:
        raise InvalidRequestError(
            f"refusing to overwrite mapped reaction list: {destination}"
        ) from exc
    return destination


def mapped_reaction_csv_fields() -> tuple[str, ...]:
    return _CANDIDATE_FIELDS
