from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from tricycle_core.errors import InvalidRequestError


@dataclass(frozen=True, slots=True)
class ReactionConfigRequest:
    reaction_id: str
    reaction_smiles: str
    method: str = "4+2"
    output_dir: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.reaction_id.strip():
            raise InvalidRequestError("reaction_id must not be empty")
        if not self.reaction_smiles.strip():
            raise InvalidRequestError("reaction_smiles must not be empty")
        if not self.method.strip():
            raise InvalidRequestError("ReactionOpt method must not be empty")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ReactionConfigRequest:
        return cls(
            reaction_id=str(payload.get("reaction_id", "")),
            reaction_smiles=str(payload.get("reaction_smiles", "")),
            method=str(payload.get("method", "4+2")),
            output_dir=payload.get("output_dir"),
            parameters=dict(payload.get("parameters", {})),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(frozen=True, slots=True)
class ReactionConfig:
    schema_version: str
    reaction_id: str
    reaction_smiles: str
    method: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "reaction_id": self.reaction_id,
            "reaction_smiles": self.reaction_smiles,
            "method": self.method,
            "parameters": dict(self.parameters),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DielsAlderSubstratePair:
    """One ene/diene pair to expand into mapped Diels–Alder reactions."""

    ene_smiles: str
    diene_smiles: str
    ene_id: str | None = None
    diene_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("ene_smiles", "diene_smiles"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise InvalidRequestError(f"{name} must not be empty")
            object.__setattr__(self, name, value)
        for name, smiles in (("ene_id", self.ene_smiles), ("diene_id", self.diene_smiles)):
            value = getattr(self, name)
            if value is None or not str(value).strip():
                digest = sha256(smiles.encode("utf-8")).hexdigest()[:12]
                value = f"{name.removesuffix('_id')}-{digest}"
            object.__setattr__(self, name, str(value).strip())

    @property
    def pair_id(self) -> str:
        return f"{self.ene_id}+{self.diene_id}"

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> DielsAlderSubstratePair:
        allowed = {"ene_id", "ene_smiles", "diene_id", "diene_smiles"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise InvalidRequestError("unknown substrate-pair fields: " + ", ".join(unknown))
        return cls(
            ene_smiles=str(payload.get("ene_smiles", "")),
            diene_smiles=str(payload.get("diene_smiles", "")),
            ene_id=(str(payload["ene_id"]) if payload.get("ene_id") is not None else None),
            diene_id=(str(payload["diene_id"]) if payload.get("diene_id") is not None else None),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "ene_id": str(self.ene_id),
            "ene_smiles": self.ene_smiles,
            "diene_id": str(self.diene_id),
            "diene_smiles": self.diene_smiles,
        }


@dataclass(frozen=True, slots=True)
class DielsAlderEnumerationRequest:
    """Request to enumerate mapped Diels–Alder reactions from substrate pairs."""

    pairs: tuple[DielsAlderSubstratePair, ...]
    stereochemistry: str = "reactant-constrained"
    max_products_per_pair: int | None = None
    max_isomers_per_product: int | None = None
    output_csv: str | None = None
    output_dir: str | None = None

    def __post_init__(self) -> None:
        pairs = tuple(
            pair
            if isinstance(pair, DielsAlderSubstratePair)
            else DielsAlderSubstratePair.from_mapping(pair)
            for pair in self.pairs
        )
        if not pairs:
            raise InvalidRequestError("at least one substrate pair is required")
        object.__setattr__(self, "pairs", pairs)
        if self.stereochemistry not in {"reactant-constrained", "all"}:
            raise InvalidRequestError(
                "stereochemistry must be 'reactant-constrained' or 'all'"
            )
        for name in ("max_products_per_pair", "max_isomers_per_product"):
            object.__setattr__(
                self, name, _positive_optional_int(getattr(self, name), name)
            )
        for name in ("output_csv", "output_dir"):
            value = getattr(self, name)
            if value is not None:
                text = str(value).strip()
                if not text:
                    raise InvalidRequestError(f"{name} must not be empty when provided")
                object.__setattr__(self, name, text)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> DielsAlderEnumerationRequest:
        allowed = {
            "pairs",
            "ene_id",
            "ene_smiles",
            "diene_id",
            "diene_smiles",
            "stereochemistry",
            "max_products_per_pair",
            "max_isomers_per_product",
            "output_csv",
            "output_dir",
            "out_dir",
            "run_dir",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise InvalidRequestError(
                "unknown reactionopt.enumerate-da fields: " + ", ".join(unknown)
            )
        pair_values = payload.get("pairs")
        if pair_values is None:
            pair_fields = {"ene_id", "ene_smiles", "diene_id", "diene_smiles"}
            pair_values = [
                {key: value for key, value in payload.items() if key in pair_fields}
            ]
        if not isinstance(pair_values, (list, tuple)):
            raise InvalidRequestError("pairs must be an array of tables")
        pairs: list[DielsAlderSubstratePair] = []
        for index, value in enumerate(pair_values):
            if not isinstance(value, Mapping):
                raise InvalidRequestError(f"pairs[{index}] must be a table")
            pairs.append(DielsAlderSubstratePair.from_mapping(value))
        output_dir = payload.get(
            "output_dir", payload.get("out_dir", payload.get("run_dir"))
        )
        return cls(
            pairs=tuple(pairs),
            stereochemistry=str(payload.get("stereochemistry", "reactant-constrained")),
            max_products_per_pair=_positive_optional_int(
                payload.get("max_products_per_pair"), "max_products_per_pair"
            ),
            max_isomers_per_product=_positive_optional_int(
                payload.get("max_isomers_per_product"), "max_isomers_per_product"
            ),
            output_csv=(
                str(payload["output_csv"])
                if payload.get("output_csv") is not None
                else None
            ),
            output_dir=(str(output_dir) if output_dir is not None else None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pairs": [pair.to_dict() for pair in self.pairs],
            "stereochemistry": self.stereochemistry,
            "max_products_per_pair": self.max_products_per_pair,
            "max_isomers_per_product": self.max_isomers_per_product,
            "output_csv": self.output_csv,
            "output_dir": self.output_dir,
        }


def _positive_optional_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvalidRequestError(f"{name} must be a positive integer when provided")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError(
            f"{name} must be a positive integer when provided"
        ) from exc
    if parsed < 1 or str(value).strip() != str(parsed):
        raise InvalidRequestError(f"{name} must be a positive integer when provided")
    return parsed


@dataclass(frozen=True, slots=True)
class ConformerSamplingConfig:
    """Configuration for one independent reactant/product conformer pool.

    The profile workflow deliberately uses the same settings for both sides by
    default, but the two pools are still embedded and optimized independently.
    ``uff`` and ``mmff94`` are provided by RDKit.  ``gfnff`` is an optional
    backend and is checked when sampling starts rather than when the package is
    imported, so the rest of TriCycle remains usable without chemistry extras.
    """

    target_conformers: int = 10
    max_conformers: int | None = None
    force_field: str = "uff"
    max_iterations: int = 200
    fmax: float = 0.01
    num_threads: int = 1
    rms_threshold: float | None = 0.5
    energy_window: float | None = None
    random_seed: int = 3407
    add_hs: bool = False

    def __post_init__(self) -> None:
        if self.target_conformers < 1:
            raise InvalidRequestError("target_conformers must be positive")
        if self.max_conformers is not None and self.max_conformers < 1:
            raise InvalidRequestError("max_conformers must be positive")
        if self.max_iterations < 1:
            raise InvalidRequestError("max_iterations must be positive")
        if self.fmax <= 0.0:
            raise InvalidRequestError("fmax must be positive")
        if self.num_threads < 1:
            raise InvalidRequestError("num_threads must be positive")
        if self.rms_threshold is not None and self.rms_threshold < 0.0:
            raise InvalidRequestError("rms_threshold must be non-negative")
        if self.energy_window is not None and self.energy_window < 0.0:
            raise InvalidRequestError("energy_window must be non-negative")
        if self.force_field not in {"uff", "mmff94", "gfnff"}:
            raise InvalidRequestError("force_field must be one of: uff, mmff94, gfnff")

    @property
    def resolved_max_conformers(self) -> int:
        return self.max_conformers or self.target_conformers

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> ConformerSamplingConfig:
        if payload is not None and not isinstance(payload, Mapping):
            raise InvalidRequestError("sampling must be a table")
        values = dict(payload or {})
        aliases = {
            "target_confs": "target_conformers",
            "max_confs": "max_conformers",
            "max_steps": "max_iterations",
            "rms_prune_threshold": "rms_threshold",
            "seed": "random_seed",
        }
        for source, target in aliases.items():
            if target not in values and source in values:
                values[target] = values[source]
        allowed = {
            "target_conformers",
            "max_conformers",
            "force_field",
            "max_iterations",
            "fmax",
            "num_threads",
            "rms_threshold",
            "energy_window",
            "random_seed",
            "add_hs",
        }
        unknown = sorted(set(values) - allowed - set(aliases))
        if unknown:
            raise InvalidRequestError(f"unknown conformer sampling fields: {', '.join(unknown)}")
        return cls(**{key: values[key] for key in allowed if key in values})

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_conformers": self.target_conformers,
            "max_conformers": self.max_conformers,
            "force_field": self.force_field,
            "max_iterations": self.max_iterations,
            "fmax": self.fmax,
            "num_threads": self.num_threads,
            "rms_threshold": self.rms_threshold,
            "energy_window": self.energy_window,
            "random_seed": self.random_seed,
            "add_hs": self.add_hs,
        }


@dataclass(frozen=True, slots=True)
class ReactionProfileRequest:
    """Request for the three-branch mapped-reaction profile workflow.

    ``reaction_smiles`` is the only required user input.  The profile boundary
    requires every atom to carry a unique positive map number and every
    hydrogen to be represented as its own mapped ``[H:...]`` atom; the actual
    chemistry validation happens when the reaction is parsed.  Reactant and
    product conformers are sampled independently, while the RitS branch
    consumes mapped reaction connectivity.  The nested ``rits`` table is intentionally opaque to this
    request model; it is parsed by the RitS task boundary and can therefore
    evolve with the upstream model without changing the mapped-reaction schema.
    """

    reaction_smiles: str
    reaction_id: str | None = None
    sampling: ConformerSamplingConfig = field(default_factory=ConformerSamplingConfig)
    rits: Mapping[str, Any] = field(default_factory=dict)
    output_dir: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        reaction_smiles = self.reaction_smiles.strip()
        if not reaction_smiles:
            raise InvalidRequestError("reaction_smiles must not be empty")
        object.__setattr__(self, "reaction_smiles", reaction_smiles)

        reaction_id = self.reaction_id
        if reaction_id is None or not str(reaction_id).strip():
            digest = sha256(reaction_smiles.encode("utf-8")).hexdigest()[:12]
            reaction_id = f"reaction-{digest}"
        object.__setattr__(self, "reaction_id", str(reaction_id).strip())

        if not isinstance(self.rits, Mapping):
            raise InvalidRequestError("rits must be a table")
        if not isinstance(self.sampling, ConformerSamplingConfig):
            if not isinstance(self.sampling, Mapping):
                raise InvalidRequestError("sampling must be a table")
            object.__setattr__(
                self,
                "sampling",
                ConformerSamplingConfig.from_mapping(self.sampling),
            )
        if self.output_dir is not None and not str(self.output_dir).strip():
            raise InvalidRequestError("output_dir must not be empty when provided")
        if not isinstance(self.metadata, Mapping):
            raise InvalidRequestError("metadata must be a table")
        object.__setattr__(self, "rits", dict(self.rits))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ReactionProfileRequest:
        allowed = {
            "reaction_smiles",
            "reaction",
            "reaction_id",
            "sampling",
            "conformers",
            "rits",
            "ts",
            "output_dir",
            "run_dir",
            "metadata",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise InvalidRequestError("unknown reactionopt.profile fields: " + ", ".join(unknown))
        sampling_value = payload.get("sampling", payload.get("conformers", {}))
        if sampling_value is None:
            sampling_value = {}
        if not isinstance(sampling_value, Mapping):
            raise InvalidRequestError("sampling must be a table")
        sampling = ConformerSamplingConfig.from_mapping(sampling_value)

        rits_value = payload.get("rits", payload.get("ts", {}))
        if rits_value is None:
            rits_value = {}
        if not isinstance(rits_value, Mapping):
            raise InvalidRequestError("rits must be a table")
        metadata_value = payload.get("metadata", {})
        if metadata_value is None:
            metadata_value = {}
        if not isinstance(metadata_value, Mapping):
            raise InvalidRequestError("metadata must be a table")

        return cls(
            reaction_smiles=str(payload.get("reaction_smiles", payload.get("reaction", ""))),
            reaction_id=(
                str(payload["reaction_id"]) if payload.get("reaction_id") is not None else None
            ),
            sampling=sampling,
            rits=rits_value,
            output_dir=(
                str(payload.get("output_dir", payload.get("run_dir")))
                if payload.get("output_dir", payload.get("run_dir")) is not None
                else None
            ),
            metadata=dict(metadata_value),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reaction_id": self.reaction_id,
            "reaction_smiles": self.reaction_smiles,
            "sampling": self.sampling.to_dict(),
            "rits": dict(self.rits),
            "output_dir": self.output_dir,
            "metadata": dict(self.metadata),
        }
