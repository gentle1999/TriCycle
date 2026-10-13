"""RDKit-backed, side-independent conformer sampling for reaction profiles."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from tricycle_core.errors import InvalidRequestError

from .models import ConformerSamplingConfig


def _rdkit() -> tuple[Any, Any, Any]:
    """Import chemistry dependencies lazily.

    The root agent keeps a dependency-light dry-run path.  Importing
    ``tricycle_reactionopt`` or listing capabilities therefore does not require
    RDKit; only an actual profile operation needs the chemistry extra.
    """

    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise InvalidRequestError(
            "ReactionOpt profile sampling requires the 'rdkit' package; "
            "install the reactionopt chemistry dependencies"
        ) from exc
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise InvalidRequestError(
            "ReactionOpt profile sampling requires the 'numpy' package"
        ) from exc
    return Chem, AllChem, np


def _copy_conformer(conf: Any) -> Any:
    Chem, _AllChem, _np = _rdkit()
    return Chem.Conformer(conf)


def _copy_pool(mol: Any, conformers: Iterable[Any]) -> Any:
    Chem, _AllChem, _np = _rdkit()
    result = Chem.Mol(mol)
    result.RemoveAllConformers()
    for conformer in conformers:
        result.AddConformer(_copy_conformer(conformer), assignId=True)
    return result


def _set_conformer_property(conformer: Any, name: str, value: object) -> None:
    if isinstance(value, bool):
        conformer.SetBoolProp(name, value)
    elif isinstance(value, int) and not isinstance(value, bool):
        conformer.SetIntProp(name, value)
    elif isinstance(value, float):
        conformer.SetDoubleProp(name, value)
    else:
        conformer.SetProp(name, str(value))


def _embed(mol: Any, config: ConformerSamplingConfig) -> Any:
    Chem, AllChem, _np = _rdkit()
    target = max(1, config.target_conformers)
    embedded = Chem.Mol(mol)
    embedded.RemoveAllConformers()

    params = AllChem.ETKDGv3()
    params.randomSeed = int(config.random_seed)
    params.numThreads = int(config.num_threads)
    # Diversity pruning is performed after optimization, where energy ordering
    # can be taken into account.  Do not let ETKDG discard candidates first.
    params.pruneRmsThresh = -1.0
    params.enforceChirality = True
    with contextlib.suppress(Exception):
        AllChem.EmbedMultipleConfs(embedded, numConfs=target, params=params)
    if embedded.GetNumConformers() == 0:
        # Some unusual aromatic or macrocyclic inputs need random coordinates.
        # RDKit can return no conformers without raising an exception.
        retry = AllChem.ETKDGv3()
        retry.randomSeed = int(config.random_seed)
        retry.numThreads = int(config.num_threads)
        retry.pruneRmsThresh = -1.0
        retry.useRandomCoords = True
        with contextlib.suppress(Exception):
            AllChem.EmbedMultipleConfs(embedded, numConfs=target, params=retry)

    if embedded.GetNumConformers() == 0:
        raise InvalidRequestError(
            "RDKit could not embed a conformer with the requested stereochemistry; "
            "check the molecular graph and stereochemical constraints"
        )
    return embedded


def _optimize_with_rdkit(mol: Any, config: ConformerSamplingConfig) -> Any:
    Chem, AllChem, _np = _rdkit()
    results: list[tuple[int, float]]
    if config.force_field == "uff":
        try:
            results = [
                (int(converged), float(energy))
                for converged, energy in AllChem.UFFOptimizeMoleculeConfs(
                    mol,
                    numThreads=int(config.num_threads),
                    maxIters=int(config.max_iterations),
                )
            ]
        except Exception as exc:
            raise InvalidRequestError(f"UFF conformer optimization failed: {exc}") from exc
    elif config.force_field == "mmff94":
        try:
            results = [
                (int(converged), float(energy))
                for converged, energy in AllChem.MMFFOptimizeMoleculeConfs(
                    mol,
                    numThreads=int(config.num_threads),
                    maxIters=int(config.max_iterations),
                )
            ]
        except Exception as exc:
            raise InvalidRequestError(f"MMFF94 conformer optimization failed: {exc}") from exc
    else:
        try:
            return _optimize_with_gfnff(mol, config, Chem, _np)
        except InvalidRequestError as exc:
            # GFNFF cannot initialize for some disconnected or otherwise
            # unsupported graphs.  The upstream ReactionOpt workflow keeps
            # reactant/product generation usable with a deterministic RDKit fallback;
            # retain the explicit missing-extra error so configuration issues
            # are not silently hidden.
            if "requires the optional 'gfnff' extra" in str(exc):
                raise
            fallback = _optimize_with_rdkit(mol, replace(config, force_field="uff"))
            for conformer in fallback.GetConformers():
                _set_conformer_property(conformer, "RequestedForceField", config.force_field)
                _set_conformer_property(conformer, "FallbackForceField", "uff")
            return fallback

    annotated: list[Any] = []
    for index, (status, energy) in enumerate(results):
        if index >= mol.GetNumConformers():
            break
        conformer = Chem.Conformer(mol.GetConformer(index))
        _set_conformer_property(conformer, "ForceField", config.force_field)
        _set_conformer_property(conformer, "Energy", energy)
        _set_conformer_property(conformer, "Converged", status == 0)
        _set_conformer_property(conformer, "EmbeddedConformerIndex", index)
        annotated.append(conformer)
    return _copy_pool(mol, annotated)


def _optimize_with_gfnff(mol: Any, config: ConformerSamplingConfig, Chem: Any, np: Any) -> Any:
    try:
        from ase import Atoms  # pyright: ignore[reportMissingImports]
        from ase.optimize import BFGS  # pyright: ignore[reportMissingImports]
        from gfnff import GFNFF  # pyright: ignore[reportMissingImports]
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise InvalidRequestError(
            "force_field = 'gfnff' requires the optional 'gfnff' extra; "
            "install tricycle-reactionopt[gfnff] or use UFF/MMFF94"
        ) from exc

    symbols = [atom.GetSymbol() for atom in mol.GetAtoms()]
    charge = sum(int(atom.GetFormalCharge()) for atom in mol.GetAtoms())
    annotated: list[Any] = []
    for index, source in enumerate(mol.GetConformers()):
        atoms = Atoms(symbols=symbols, positions=np.asarray(source.GetPositions(), dtype=float))
        try:
            atoms.calc = GFNFF(charge=charge)
            optimizer = BFGS(atoms, logfile=None)
            converged = bool(optimizer.run(fmax=config.fmax, steps=config.max_iterations))
            energy = float(atoms.get_potential_energy())
            positions = np.asarray(atoms.get_positions(), dtype=float)
        except Exception as exc:
            raise InvalidRequestError(
                f"GFNFF conformer optimization failed for conformer {index}: {exc}"
            ) from exc
        if not np.all(np.isfinite(positions)) or not np.isfinite(energy):
            continue
        conformer = Chem.Conformer(source)
        conformer.SetPositions(positions)
        _set_conformer_property(conformer, "ForceField", config.force_field)
        _set_conformer_property(conformer, "Energy", energy)
        _set_conformer_property(conformer, "Converged", converged)
        _set_conformer_property(conformer, "EmbeddedConformerIndex", index)
        annotated.append(conformer)
    return _copy_pool(mol, annotated)


def _energy(conformer: Any) -> float:
    try:
        return float(conformer.GetDoubleProp("Energy"))
    except Exception:
        return float("inf")


def _aligned_rms(moving: Any, reference: Any, np: Any) -> float:
    if moving.shape != reference.shape:
        return float("inf")
    if moving.size == 0:
        return 0.0
    moving_centered = moving - moving.mean(axis=0)
    reference_centered = reference - reference.mean(axis=0)
    covariance = moving_centered.T @ reference_centered
    left, _singular, right_t = np.linalg.svd(covariance)
    rotation = left @ right_t
    if np.linalg.det(rotation) < 0.0:
        left[:, -1] *= -1.0
        rotation = left @ right_t
    difference = moving_centered @ rotation - reference_centered
    return float(np.sqrt(np.mean(np.sum(difference * difference, axis=1))))


def _prune_pool(mol: Any, config: ConformerSamplingConfig) -> Any:
    _Chem, _AllChem, np = _rdkit()
    conformers = list(mol.GetConformers())
    ordered = sorted(enumerate(conformers), key=lambda item: (_energy(item[1]), item[0]))
    if config.energy_window is not None and ordered:
        minimum = _energy(ordered[0][1])
        ordered = [
            item for item in ordered if _energy(item[1]) <= minimum + float(config.energy_window)
        ]

    kept: list[Any] = []
    threshold = config.rms_threshold
    for _index, candidate in ordered:
        if threshold is not None and threshold > 0.0:
            candidate_positions = np.asarray(candidate.GetPositions(), dtype=float)
            if any(
                _aligned_rms(
                    candidate_positions,
                    np.asarray(previous.GetPositions(), dtype=float),
                    np,
                )
                < threshold
                for previous in kept
            ):
                continue
        kept.append(candidate)
        if len(kept) >= config.resolved_max_conformers:
            break
    return _copy_pool(mol, kept)


def sample_conformer_ensemble(
    mol: Any,
    config: ConformerSamplingConfig | None = None,
    *,
    side: str = "reaction",
) -> Any:
    """Embed, optimize, rank, and prune one complete reaction side.

    Reactants and products are passed to this function separately by the
    profile builder.  Keeping the sides independent is important: their
    disconnected-fragment arrangements are not used to manufacture a TS
    guess, and no product-to-reactant reverse construction is performed.
    """

    Chem, _AllChem, _np = _rdkit()
    if mol is None:
        raise InvalidRequestError(f"{side} molecule must not be None")
    if mol.GetNumAtoms() < 1:
        raise InvalidRequestError(f"{side} molecule must contain at least one atom")
    resolved = config or ConformerSamplingConfig()
    working = Chem.Mol(mol)
    working.RemoveAllConformers()
    # GFNFF's calculator needs explicit hydrogens.  A profile input already
    # contains independently mapped [H] atoms, so those atoms must remain in
    # the returned pool for RitS; only implicit-H convenience inputs may use
    # an internal add/remove-H round trip.
    has_explicit_h_atoms = any(atom.GetAtomicNum() == 1 for atom in working.GetAtoms())
    strip_internal_hs = (
        resolved.force_field == "gfnff" and not resolved.add_hs and not has_explicit_h_atoms
    )
    if resolved.add_hs or resolved.force_field == "gfnff":
        working = Chem.AddHs(working)
    embedded = _embed(working, resolved)
    optimized = _optimize_with_rdkit(embedded, resolved)
    if optimized.GetNumConformers() == 0:
        raise InvalidRequestError(f"no {side} conformers remained after optimization")
    result = _prune_pool(optimized, resolved)
    if result.GetNumConformers() == 0:
        raise InvalidRequestError(f"no {side} conformers remained after pruning")
    if strip_internal_hs:
        result = Chem.RemoveHs(result)
    result.SetProp("TriCycleSide", side)
    actual_force_field = resolved.force_field
    if result.GetNumConformers() > 0:
        conformer = result.GetConformer(0)
        if conformer.HasProp("ForceField"):
            actual_force_field = conformer.GetProp("ForceField")
    result.SetProp("TriCycleForceField", actual_force_field)
    result.SetProp("TriCycleRequestedForceField", resolved.force_field)
    return result


def single_conformer(mol: Any, conformer_index: int) -> Any:
    """Return an independent one-conformer molecule from a conformer pool."""

    Chem, _AllChem, _np = _rdkit()
    if conformer_index < 0 or conformer_index >= mol.GetNumConformers():
        raise InvalidRequestError(f"conformer index out of range: {conformer_index}")
    result = Chem.Mol(mol)
    conformer = Chem.Conformer(mol.GetConformer(conformer_index))
    result.RemoveAllConformers()
    result.AddConformer(conformer, assignId=True)
    return result


__all__ = ["sample_conformer_ensemble", "single_conformer"]
