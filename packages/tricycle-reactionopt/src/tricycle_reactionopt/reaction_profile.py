"""Mapped-reaction profile construction for ReactionOpt.

This module keeps the topology/profile model independent from a particular TS
transport.  RDKit is loaded lazily because the rest of the TriCycle agent has a
dependency-light dry-run path.  Profile inputs must contain complete explicit
hydrogens as separately mapped ``[H:...]`` atoms, with a unique positive map on
every atom and the same mapped atom set on both sides.  A profile fans out into
three independent branches: reactant conformer sampling, product conformer
sampling, and reaction-level RitS transition-state sampling.  RitS receives
mapped reactant/product connectivity and is invoked once per reaction.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from tricycle_core.errors import InvalidRequestError

from .models import ConformerSamplingConfig
from .rits import (
    RitsInferenceConfig,
    RitsTransitionStateInput,
    RitsTransitionStateResult,
    RitsTransitionStateSampler,
    molecule_to_xyz,
    normalize_transition_state_structures,
)


def _chem() -> Any:
    try:
        from rdkit import Chem
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise InvalidRequestError(
            "mapped reaction profiles require the 'rdkit' package; "
            "install the reactionopt chemistry dependencies"
        ) from exc
    return Chem


def _numpy() -> Any:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise InvalidRequestError("mapped reaction profiles require the 'numpy' package") from exc
    return np


def _split_reaction_smiles(reaction_smiles: str) -> tuple[str, str]:
    text = str(reaction_smiles).strip()
    if ">>" in text:
        parts = text.split(">>")
        if len(parts) != 2:
            raise InvalidRequestError("reaction_smiles must contain one >> separator")
        reactants, products = parts
    else:
        parts = text.split(">")
        if len(parts) != 3:
            raise InvalidRequestError(
                "reaction_smiles must use reactants>>products or reactants>agents>products"
            )
        reactants, _agents, products = parts
    if not reactants.strip() or not products.strip():
        raise InvalidRequestError("reaction_smiles must include reactants and products")
    return reactants.strip(), products.strip()


def _parse_side(smiles: str, side: str) -> Any:
    Chem = _chem()
    params = Chem.SmilesParserParams()
    params.removeHs = False
    molecule = Chem.MolFromSmiles(smiles, params)
    if molecule is None:
        raise InvalidRequestError(f"could not parse mapped {side} SMILES")
    return molecule


def _side_fragments(molecule: Any) -> tuple[Any, ...]:
    Chem = _chem()
    try:
        return tuple(Chem.GetMolFrags(molecule, asMols=True, sanitizeFrags=True))
    except Exception as exc:
        raise InvalidRequestError(f"could not split reaction side into molecules: {exc}") from exc


def _mapped_order(molecule: Any, side: str) -> tuple[int, ...]:
    map_numbers: list[int] = []
    for atom_index, atom in enumerate(molecule.GetAtoms()):
        map_number = int(atom.GetAtomMapNum())
        if atom.GetNumImplicitHs() > 0:
            raise InvalidRequestError(
                f"{side} atom {atom_index} (map {map_number or 'unset'}) has implicit "
                "hydrogens; use separate mapped [H:...] atoms"
            )
        if atom.GetNumExplicitHs() > 0:
            raise InvalidRequestError(
                f"{side} atom {atom_index} (map {map_number or 'unset'}) stores hydrogen "
                "on a heavy atom; use separate mapped [H:...] atoms"
            )
        map_numbers.append(map_number)
    if any(number <= 0 for number in map_numbers):
        raise InvalidRequestError(f"all {side} atoms must have a positive atom-map number")
    if len(set(map_numbers)) != len(map_numbers):
        raise InvalidRequestError(f"{side} atom-map numbers must be unique")
    return tuple(
        index for index, _number in sorted(enumerate(map_numbers), key=lambda item: item[1])
    )


def _renumber_by_map(molecule: Any, side: str) -> tuple[Any, tuple[int, ...]]:
    Chem = _chem()
    order = _mapped_order(molecule, side)
    return Chem.RenumberAtoms(molecule, list(order)), order


def _copy_molecule(molecule: Any) -> Any:
    Chem = _chem()
    return Chem.Mol(molecule)


def _single_conformer_copy(molecule: Any, conformer_index: int) -> Any:
    Chem = _chem()
    if conformer_index < 0 or conformer_index >= molecule.GetNumConformers():
        raise InvalidRequestError(f"conformer index out of range: {conformer_index}")
    result = Chem.Mol(molecule)
    conformer = Chem.Conformer(molecule.GetConformer(conformer_index))
    result.RemoveAllConformers()
    result.AddConformer(conformer, assignId=True)
    return result


def _single_conformer_required(molecule: Any, label: str) -> None:
    if molecule is None or molecule.GetNumConformers() != 1:
        raise InvalidRequestError(f"{label} must contain exactly one conformer")


@dataclass(slots=True)
class Species:
    """One mapped molecule on a reaction side."""

    mol: Any
    side: Literal["reactants", "products"]
    index: int

    @classmethod
    def from_smiles(
        cls,
        smiles: str,
        *,
        side: Literal["reactants", "products"] = "reactants",
        index: int = 0,
    ) -> Species:
        return cls(_parse_side(smiles, side), side, index)

    @property
    def is_optimized(self) -> bool:
        return any(
            conf.HasProp("ForceField") and conf.HasProp("Energy")
            for conf in self.mol.GetConformers()
        )

    def optimize(
        self,
        config: ConformerSamplingConfig | None = None,
    ) -> Species:
        from .conformers import sample_conformer_ensemble

        resolved = config or ConformerSamplingConfig()
        self.mol = sample_conformer_ensemble(self.mol, resolved, side=self.side)
        return self

    @classmethod
    def from_mol(
        cls,
        molecule: Any,
        *,
        side: Literal["reactants", "products"] = "reactants",
        index: int = 0,
    ) -> Species:
        return cls(_copy_molecule(molecule), side, index)

    @property
    def iter_mol_with_conformer(self) -> tuple[Any, ...]:
        return tuple(
            _single_conformer_copy(self.mol, index) for index in range(self.mol.GetNumConformers())
        )

    def get_mol_with_conformer(self, conformer_index: int) -> Any:
        return _single_conformer_copy(self.mol, conformer_index)


@dataclass(frozen=True, slots=True)
class ReactionPathContext:
    """Optional topology context for downstream path-oriented adapters."""

    reaction: Reaction
    reactants: Any
    products: Any
    diff_reactants: Any
    diff_products: Any
    reactants_bond_matrix: Any
    products_bond_matrix: Any
    reaction_bond_matrix: Any
    changed_bonds: tuple[tuple[int, int], ...]
    forming_bonds: tuple[tuple[int, int], ...]
    breaking_bonds: tuple[tuple[int, int], ...]
    reactants_template: Any
    products_template: Any
    reactants_seed: Any
    products_seed: Any
    reactants_stereo_reference: Any


@dataclass(frozen=True, slots=True)
class TransitionStateConformer:
    mol: Any
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _single_conformer_required(self.mol, "transition-state conformer")
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class TransitionStateConformerSet:
    conformers: tuple[TransitionStateConformer, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.conformers:
            raise InvalidRequestError("transition-state conformer set must not be empty")
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def mols(self) -> tuple[Any, ...]:
        return tuple(item.mol for item in self.conformers)


@dataclass(frozen=True, slots=True)
class ReactionProfile:
    """Complete mapped reaction profile produced by three independent branches."""

    reaction: Reaction
    reactant_conformers: Any
    product_conformers: Any
    transition_state_conformer_set: TransitionStateConformerSet
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        expected_atom_maps = self.reaction.atom_map_numbers
        _validate_structure_atom_mapping(
            self.reactant_conformers,
            expected_atom_maps,
            "reactant conformer pool",
        )
        _validate_structure_atom_mapping(
            self.product_conformers,
            expected_atom_maps,
            "product conformer pool",
        )
        for index, conformer in enumerate(self.transition_state_conformer_set.conformers):
            _validate_structure_atom_mapping(
                conformer.mol,
                expected_atom_maps,
                f"transition-state conformer {index}",
            )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def reactants(self) -> Any:
        return self.reactant_conformers

    @property
    def products(self) -> Any:
        return self.product_conformers

    @property
    def transition_states(self) -> tuple[Any, ...]:
        return self.transition_state_conformer_set.mols

    @property
    def atom_map_numbers(self) -> tuple[int, ...]:
        """Return the global atom-map order shared by all profile branches."""

        return self.reaction.atom_map_numbers

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "tricycle.reactionopt/profile-v1",
            "atom_map_numbers": list(self.atom_map_numbers),
            "atom_order": "ascending-atom-map",
            "reaction": self.reaction.to_dict(),
            "reactant_conformer_count": self.reactant_conformers.GetNumConformers(),
            "product_conformer_count": self.product_conformers.GetNumConformers(),
            "transition_state_conformer_count": len(self.transition_state_conformer_set.conformers),
            "metadata": _json_safe(self.metadata),
        }

    def save(
        self,
        output_dir: str | Path,
        *,
        file_format: Literal["sdf", "xyz", "both"] = "sdf",
    ) -> tuple[Path, ...]:
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        formats = ("sdf", "xyz") if file_format == "both" else (file_format,)
        written: list[Path] = []
        for label, molecule in (
            ("reactants", self.reactant_conformers),
            ("products", self.product_conformers),
        ):
            for output_format in formats:
                destination = root / f"{label}.{output_format}"
                _write_structure(molecule, destination, output_format)
                written.append(destination)
        for output_format in formats:
            destination = root / f"transition_states.{output_format}"
            _write_structure_sequence(
                self.transition_state_conformer_set.mols,
                destination,
                output_format,
            )
            written.append(destination)
        metadata_path = root / "profile.json"
        _write_json(metadata_path, self.to_dict())
        written.append(metadata_path)
        return tuple(written)


class Reaction:
    """A validated atom-mapped reaction and its generated profile state."""

    def __init__(
        self,
        reaction_smiles: str,
        reactants: Sequence[Any],
        products: Sequence[Any],
        *,
        reactant_mapped_molecule: Any | None = None,
        product_mapped_molecule: Any | None = None,
    ) -> None:
        self.reaction_smiles = str(reaction_smiles).strip()
        if not self.reaction_smiles:
            raise InvalidRequestError("reaction_smiles must not be empty")
        self.reactants = tuple(
            Species(molecule, "reactants", index) for index, molecule in enumerate(reactants)
        )
        self.products = tuple(
            Species(molecule, "products", index) for index, molecule in enumerate(products)
        )
        if not self.reactants or not self.products:
            raise InvalidRequestError("reaction must contain reactants and products")

        self._reactant_raw = (
            reactant_mapped_molecule
            if reactant_mapped_molecule is not None
            else _combine_species(self.reactants)
        )
        self._product_raw = (
            product_mapped_molecule
            if product_mapped_molecule is not None
            else _combine_species(self.products)
        )
        self._merged_reactants, self._reactant_order = _renumber_by_map(
            self._reactant_raw, "reactant"
        )
        self._merged_products, self._product_order = _renumber_by_map(self._product_raw, "product")
        self._validate_atom_mapping()
        self.transition_state_conformer_set: TransitionStateConformerSet | None = None
        self._path_context: ReactionPathContext | None = None

    @classmethod
    def from_rxn_smiles(cls, reaction_smiles: str) -> Reaction:
        reactant_smiles, product_smiles = _split_reaction_smiles(reaction_smiles)
        reactant_molecule = _parse_side(reactant_smiles, "reactant")
        product_molecule = _parse_side(product_smiles, "product")
        return cls(
            reaction_smiles,
            _side_fragments(reactant_molecule),
            _side_fragments(product_molecule),
            reactant_mapped_molecule=reactant_molecule,
            product_mapped_molecule=product_molecule,
        )

    @classmethod
    def from_reactants_products(
        cls,
        reactants: Sequence[Any],
        products: Sequence[Any],
    ) -> Reaction:
        reactant_molecule = _combine_molecules(reactants)
        product_molecule = _combine_molecules(products)
        reaction_smiles = (
            f"{_chem().MolToSmiles(reactant_molecule, canonical=False)}>>"
            f"{_chem().MolToSmiles(product_molecule, canonical=False)}"
        )
        return cls(
            reaction_smiles,
            _side_fragments(reactant_molecule),
            _side_fragments(product_molecule),
            reactant_mapped_molecule=reactant_molecule,
            product_mapped_molecule=product_molecule,
        )

    @classmethod
    def from_rdReaction(cls, reaction: Any) -> Reaction:
        """Build a profile model from an RDKit ``ChemicalReaction`` object."""

        return cls.from_reactants_products(
            reaction.GetReactants(),
            reaction.GetProducts(),
        )

    @property
    def merged_reactants(self) -> Any:
        return _copy_molecule(self._merged_reactants)

    @property
    def merged_products(self) -> Any:
        return _copy_molecule(self._merged_products)

    @property
    def reactant_smiles(self) -> str:
        """Return the mapped reactant connectivity consumed by RitS."""

        return _chem().MolToSmiles(self._merged_reactants, canonical=False)

    @property
    def product_smiles(self) -> str:
        """Return the mapped product connectivity consumed by RitS."""

        return _chem().MolToSmiles(self._merged_products, canonical=False)

    @property
    def reactants_mapping(self) -> list[int]:
        return list(self._reactant_order)

    @property
    def products_mapping(self) -> list[int]:
        return list(self._product_order)

    @property
    def atom_map_numbers(self) -> tuple[int, ...]:
        return tuple(int(atom.GetAtomMapNum()) for atom in self._merged_reactants.GetAtoms())

    @property
    def reactants_bond_matrix(self) -> Any:
        return _bond_matrix(self._merged_reactants)

    @property
    def products_bond_matrix(self) -> Any:
        return _bond_matrix(self._merged_products)

    @property
    def reaction_bond_matrix(self) -> Any:
        return self.products_bond_matrix - self.reactants_bond_matrix

    @property
    def diff_reactants_mol(self) -> Any:
        return _add_zero_order_bonds(self.merged_reactants, self.changed_bonds)

    @property
    def diff_products_mol(self) -> Any:
        return _add_zero_order_bonds(self.merged_products, self.changed_bonds)

    @property
    def reactants_stereo_reference(self) -> Any:
        return self.merged_reactants

    @property
    def changed_bonds(self) -> Any:
        np = _numpy()
        rows, columns = np.nonzero(self.reaction_bond_matrix)
        return np.asarray(
            [
                (int(row), int(column))
                for row, column in zip(rows, columns, strict=True)
                if int(row) < int(column)
            ],
            dtype=int,
        ).reshape((-1, 2))

    @property
    def unchanged_bonds(self) -> Any:
        np = _numpy()
        rows, columns = np.nonzero(self.reaction_bond_matrix == 0)
        return np.asarray(
            [
                (int(row), int(column))
                for row, column in zip(rows, columns, strict=True)
                if int(row) < int(column)
            ],
            dtype=int,
        ).reshape((-1, 2))

    @property
    def forming_bonds(self) -> tuple[tuple[int, int], ...]:
        return _changed_bonds_by_sign(self.reaction_bond_matrix, positive=True)

    @property
    def breaking_bonds(self) -> tuple[tuple[int, int], ...]:
        return _changed_bonds_by_sign(self.reaction_bond_matrix, positive=False)

    @property
    def searched_transition_state_conformers(self) -> tuple[Any, ...]:
        if self.transition_state_conformer_set is None:
            return ()
        return self.transition_state_conformer_set.mols

    def build_reaction_path_context(self) -> ReactionPathContext:
        reactants = self.merged_reactants
        products = self.merged_products
        diff_reactants = _add_zero_order_bonds(reactants, self.changed_bonds)
        diff_products = _add_zero_order_bonds(products, self.changed_bonds)
        context = ReactionPathContext(
            reaction=self,
            reactants=reactants,
            products=products,
            diff_reactants=diff_reactants,
            diff_products=diff_products,
            reactants_bond_matrix=self.reactants_bond_matrix,
            products_bond_matrix=self.products_bond_matrix,
            reaction_bond_matrix=self.reaction_bond_matrix,
            changed_bonds=_tuple_edges(self.changed_bonds),
            forming_bonds=self.forming_bonds,
            breaking_bonds=self.breaking_bonds,
            reactants_template=_copy_molecule(reactants),
            products_template=_copy_molecule(products),
            reactants_seed=_copy_molecule(reactants),
            products_seed=_copy_molecule(products),
            reactants_stereo_reference=_copy_molecule(reactants),
        )
        self._path_context = context
        return context

    def sample_reactant_conformers(
        self,
        config: ConformerSamplingConfig | None = None,
    ) -> Any:
        """Sample the reactant-side branch."""

        from .conformers import sample_conformer_ensemble

        resolved = config or ConformerSamplingConfig()
        return sample_conformer_ensemble(
            self._merged_reactants,
            resolved,
            side="reactants",
        )

    def sample_product_conformers(
        self,
        config: ConformerSamplingConfig | None = None,
    ) -> Any:
        """Sample the product-side branch."""

        from .conformers import sample_conformer_ensemble

        resolved = config or ConformerSamplingConfig()
        return sample_conformer_ensemble(
            self._merged_products,
            resolved,
            side="products",
        )

    def plan_rits_jobs(
        self,
        *,
        rits: RitsInferenceConfig | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Describe the single reaction-level RitS job without sampling."""

        rits_config = rits or RitsInferenceConfig()
        output = (
            rits_config.pipeline.output_dir
            if rits_config.pipeline.enabled
            else "outputs/reaction-ts.xyz"
        )
        job = rits_config.to_bohr_job(
            job_name="reactionopt-rits-ts",
            reaction_smiles=self.reaction_smiles,
            output=output,
        )
        return (
            {
                "reaction_smiles": self.reaction_smiles,
                "reactant_smiles": self.reactant_smiles,
                "product_smiles": self.product_smiles,
                "output": output,
                "output_kind": "directory" if rits_config.pipeline.enabled else "file",
                "job": job.to_dict(),
            },
        )

    def sample_transition_states(
        self,
        sampler: RitsTransitionStateSampler,
        *,
        rits: RitsInferenceConfig | None = None,
    ) -> TransitionStateConformerSet:
        """Run the injected RitS sampler once for the mapped reaction."""

        rits_config = rits or RitsInferenceConfig()
        request = RitsTransitionStateInput(
            reaction_smiles=self.reaction_smiles,
            config=rits_config,
            reactant_smiles=self.reactant_smiles,
            product_smiles=self.product_smiles,
        )
        if not callable(getattr(sampler, "sample", None)):
            raise InvalidRequestError("RitS sampler must implement sample(request)")
        rits_result = sampler.sample(request)
        if not isinstance(rits_result, RitsTransitionStateResult):
            raise InvalidRequestError("RitS sampler must return RitsTransitionStateResult")
        structures = normalize_transition_state_structures(
            rits_result.structures,
            self._merged_reactants,
        )
        ts_conformers = [
            TransitionStateConformer(
                structure,
                {
                    **dict(rits_result.metadata),
                    "builder_name": "rits-transition-state-sampling",
                    "builder_type": "external-model",
                    "rits_sample_index": sample_index,
                    "rits": rits_config.to_dict(),
                },
            )
            for sample_index, structure in enumerate(structures)
        ]
        if not ts_conformers:
            raise InvalidRequestError("RitS returned no transition-state conformers")
        transition_state_set = TransitionStateConformerSet(
            tuple(ts_conformers),
            {
                "builder_name": "rits-transition-state-sampling",
                "builder_type": "external-model",
                "inference_count": 1,
                "ts_pipeline": rits_config.pipeline.to_dict(),
                "pipeline_result": dict(rits_result.metadata),
                "rits": rits_config.to_dict(),
            },
        )
        self.transition_state_conformer_set = transition_state_set
        return transition_state_set

    def build_profile(
        self,
        sampler: RitsTransitionStateSampler,
        *,
        sampling: ConformerSamplingConfig | None = None,
        rits: RitsInferenceConfig | None = None,
    ) -> ReactionProfile:
        """Run three independent branches and aggregate their results."""

        reactant_pool = self.sample_reactant_conformers(sampling)
        product_pool = self.sample_product_conformers(sampling)
        transition_states = self.sample_transition_states(
            sampler,
            rits=rits,
        )
        return ReactionProfile(
            reaction=self,
            reactant_conformers=reactant_pool,
            product_conformers=product_pool,
            transition_state_conformer_set=transition_states,
            metadata={
                "builder_name": "mapped-reaction-profile",
                "reactant_product_conformer_sampling": "independent",
                "ts_sampling": "rits.run_ts_pipeline",
                "ts_pipeline": dict(transition_states.metadata),
            },
        )

    def _validate_atom_mapping(self) -> None:
        reactant_atoms = list(self._merged_reactants.GetAtoms())
        product_atoms = list(self._merged_products.GetAtoms())
        if len(reactant_atoms) != len(product_atoms):
            raise InvalidRequestError(
                "mapped reaction must have the same number of reactant and product atoms"
            )
        reactant_maps = [int(atom.GetAtomMapNum()) for atom in reactant_atoms]
        product_maps = [int(atom.GetAtomMapNum()) for atom in product_atoms]
        if reactant_maps != product_maps:
            raise InvalidRequestError(
                "reactant and product atom-map numbers must match after map ordering"
            )
        for reactant_atom, product_atom in zip(reactant_atoms, product_atoms, strict=True):
            if reactant_atom.GetAtomicNum() != product_atom.GetAtomicNum():
                raise InvalidRequestError(
                    "reactant and product atoms with the same map number must have the same element"
                )

    def to_dict(self) -> dict[str, Any]:
        Chem = _chem()
        return {
            "schema_version": "tricycle.reactionopt/mapped-reaction-v1",
            "reaction_smiles": self.reaction_smiles,
            "reactant_smiles": Chem.MolToSmiles(self._merged_reactants, canonical=False),
            "product_smiles": Chem.MolToSmiles(self._merged_products, canonical=False),
            "atom_map_numbers": list(self.atom_map_numbers),
            "num_atoms": self._merged_reactants.GetNumAtoms(),
            "changed_bonds": [list(edge) for edge in _tuple_edges(self.changed_bonds)],
            "forming_bonds": [list(edge) for edge in self.forming_bonds],
            "breaking_bonds": [list(edge) for edge in self.breaking_bonds],
            "reactants_bond_matrix": self.reactants_bond_matrix.tolist(),
            "products_bond_matrix": self.products_bond_matrix.tolist(),
            "complete_atom_mapping": True,
            "explicit_hydrogens": True,
            "hydrogen_representation": "separate-mapped-atoms",
            "atom_order_is_mapped": True,
            "ts_builder": "rits",
        }




def _combine_species(species: Sequence[Species]) -> Any:
    return _combine_molecules(tuple(item.mol for item in species))


def _combine_molecules(molecules: Sequence[Any]) -> Any:
    Chem = _chem()
    if not molecules:
        return Chem.Mol()
    result = Chem.Mol(molecules[0])
    result.RemoveAllConformers()
    for molecule in molecules[1:]:
        result = Chem.CombineMols(result, molecule)
    result.RemoveAllConformers()
    return result


def _bond_matrix(molecule: Any) -> Any:
    np = _numpy()
    matrix = np.zeros((molecule.GetNumAtoms(), molecule.GetNumAtoms()), dtype=float)
    for bond in molecule.GetBonds():
        value = float(bond.GetBondTypeAsDouble())
        begin, end = int(bond.GetBeginAtomIdx()), int(bond.GetEndAtomIdx())
        matrix[begin, end] = value
        matrix[end, begin] = value
    return matrix


def _changed_bonds_by_sign(
    matrix: Any,
    *,
    positive: bool,
) -> tuple[tuple[int, int], ...]:
    count = int(matrix.shape[0])
    result: list[tuple[int, int]] = []
    for begin in range(count):
        for end in range(begin + 1, count):
            delta = float(matrix[begin, end])
            if (positive and delta > 0.0) or (not positive and delta < 0.0):
                result.append((begin, end))
    return tuple(result)


def _tuple_edges(edges: Any) -> tuple[tuple[int, int], ...]:
    return tuple((int(begin), int(end)) for begin, end in edges.tolist())


def _validate_structure_atom_mapping(
    molecule: Any,
    expected_atom_maps: tuple[int, ...],
    label: str,
) -> None:
    if molecule is None:
        raise InvalidRequestError(f"{label} must not be None")
    actual_atom_maps = tuple(int(atom.GetAtomMapNum()) for atom in molecule.GetAtoms())
    if actual_atom_maps != expected_atom_maps:
        raise InvalidRequestError(
            f"{label} must preserve the full reaction atom mapping and mapped atom order"
        )


def _add_zero_order_bonds(molecule: Any, changed_bonds: Any) -> Any:
    Chem = _chem()
    result = Chem.RWMol(molecule)
    for begin, end in changed_bonds.tolist():
        if result.GetBondBetweenAtoms(int(begin), int(end)) is None:
            result.AddBond(int(begin), int(end), Chem.BondType.ZERO)
    with contextlib.suppress(Exception):
        Chem.SanitizeMol(result)
        # Zero-order bonds are deliberately non-valence bonds.  RDKit versions
        # differ in how much sanitization they permit, so keep the graph if the
        # optional normalization rejects it.
    return result.GetMol()


def _write_structure(molecule: Any, destination: Path, output_format: str) -> None:
    if output_format == "xyz":
        _write_xyz_pool(molecule, destination)
    elif output_format == "sdf":
        _write_sdf_pool(molecule, destination)
    else:
        raise InvalidRequestError(f"unsupported structure output format: {output_format}")


def _write_xyz_pool(molecule: Any, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for index in range(molecule.GetNumConformers()):
        blocks.append(
            molecule_to_xyz(_single_conformer_copy(molecule, index), comment=f"conformer {index}")
        )
    destination.write_text("\n".join(blocks), encoding="utf-8")


def _write_sdf_pool(molecule: Any, destination: Path) -> None:
    Chem = _chem()
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer = Chem.SDWriter(str(destination))
    try:
        for index in range(molecule.GetNumConformers()):
            writer.write(_single_conformer_copy(molecule, index))
    finally:
        writer.close()


def _write_structure_sequence(
    molecules: Sequence[Any], destination: Path, output_format: str
) -> None:
    if not molecules:
        raise InvalidRequestError("structure sequence must not be empty")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "xyz":
        destination.write_text(
            "\n".join(
                molecule_to_xyz(molecule, comment=f"conformer {index}")
                for index, molecule in enumerate(molecules)
            ),
            encoding="utf-8",
        )
        return
    if output_format == "sdf":
        Chem = _chem()
        writer = Chem.SDWriter(str(destination))
        try:
            for molecule in molecules:
                if molecule is None or molecule.GetNumConformers() != 1:
                    raise InvalidRequestError(
                        "each structure in a sequence must contain exactly one conformer"
                    )
                writer.write(molecule)
        finally:
            writer.close()
        return
    raise InvalidRequestError(f"unsupported structure output format: {output_format}")


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "Reaction",
    "ReactionPathContext",
    "ReactionProfile",
    "Species",
    "TransitionStateConformer",
    "TransitionStateConformerSet",
]
