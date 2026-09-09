"""RitS transition-state sampling boundary used by ReactionOpt profiles."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from tricycle_core import Artifact, BohrJobSpec
from tricycle_core.errors import InvalidRequestError

DEFAULT_RITS_IMAGE = (
    "registry.dp.tech/dptech/dp/native/prod-23278/ubuntu2404-py312-pt212-cu130-dl:260831"
)
DEFAULT_RITS_SCRIPT = "scripts/sample_transition_state.py"
DEFAULT_RITS_PIPELINE_SCRIPT = "scripts/run_ts_pipeline.py"
DEFAULT_RITS_CONFIG = "scripts/conf/rits.yaml"
DEFAULT_RITS_CHECKPOINT = "data/rits.ckpt"
DEFAULT_RITS_CHECKPOINT_SOURCE = "artifacts/rits/rits.ckpt"


def _text(value: object, name: str, *, default: str | None = None) -> str:
    resolved = default if value is None else str(value)
    if resolved is None or not resolved.strip():
        raise InvalidRequestError(f"{name} must not be empty")
    return resolved.strip()


def _artifact(
    value: object,
    *,
    name: str,
    default_location: str | None = None,
    default_kind: str = "file",
) -> Artifact | None:
    if value is None or value == "":
        if default_location is None:
            return None
        return Artifact(f"rits-{name}", default_location, kind=default_kind)
    if isinstance(value, Artifact):
        return value
    if isinstance(value, (str, Path)):
        return Artifact(f"rits-{name}", str(value), kind=default_kind)
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{name} must be an artifact or path")
    try:
        return Artifact(**dict(value))
    except TypeError as exc:
        raise InvalidRequestError(f"{name} must contain artifact_id and location") from exc


def _artifact_list(value: object) -> tuple[Artifact, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise InvalidRequestError("rits.output_artifacts must be a list")
    result: list[Artifact] = []
    for index, item in enumerate(value):
        parsed = _artifact(item, name=f"output-{index}")
        if parsed is not None:
            result.append(parsed)
    return tuple(result)


def _extra_args(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise InvalidRequestError("rits.extra_args must be a list")
    result = tuple(str(item) for item in value)
    if any(not item.strip() for item in result):
        raise InvalidRequestError("rits.extra_args entries must not be empty")
    return result


def _split_reaction_smiles(reaction_smiles: str) -> tuple[str, str]:
    text = _text(reaction_smiles, "reaction_smiles")
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
    return _text(reactants, "reactants"), _text(products, "products")


def _resolve_reaction_inputs(
    *,
    reaction_smiles: str | None,
    reactant_smiles: str | None,
    product_smiles: str | None,
) -> tuple[str, str]:
    if reaction_smiles is not None:
        if reactant_smiles is not None or product_smiles is not None:
            raise InvalidRequestError(
                "provide reaction_smiles or reactant_smiles/product_smiles, not both"
            )
        return _split_reaction_smiles(reaction_smiles)
    if reactant_smiles is None or product_smiles is None:
        raise InvalidRequestError(
            "provide reaction_smiles or both reactant_smiles and product_smiles"
        )
    return _text(reactant_smiles, "reactant_smiles"), _text(product_smiles, "product_smiles")


def _pipeline_reaction_smiles(reactant_smiles: str, product_smiles: str) -> str:
    """Return a pipeline-safe reaction with contiguous internal map indices.

    The configured fork pipeline preprocessing indexes mapped atoms with ``map - 1``.
    Profile output still uses the original map numbers; this reindexing is
    only for the worker input and the selected XYZ atom order.
    """

    Chem = _chem()
    params = Chem.SmilesParserParams()
    params.removeHs = False
    reactants = Chem.MolFromSmiles(reactant_smiles, params)
    products = Chem.MolFromSmiles(product_smiles, params)
    if reactants is None or products is None:
        raise InvalidRequestError("could not parse mapped reaction for the RitS pipeline")

    reactant_atoms = list(reactants.GetAtoms())
    product_atoms = list(products.GetAtoms())
    for side, atoms in (("reactant", reactant_atoms), ("product", product_atoms)):
        for atom in atoms:
            if atom.GetNumImplicitHs() > 0 or atom.GetNumExplicitHs() > 0:
                raise InvalidRequestError(
                    f"RitS pipeline {side} input must represent every hydrogen as a "
                    "separate mapped [H:...] atom"
                )
    reactant_maps = [int(atom.GetAtomMapNum()) for atom in reactant_atoms]
    product_maps = [int(atom.GetAtomMapNum()) for atom in product_atoms]
    if (
        any(number <= 0 for number in reactant_maps + product_maps)
        or len(set(reactant_maps)) != len(reactant_maps)
        or len(set(product_maps)) != len(product_maps)
        or set(reactant_maps) != set(product_maps)
    ):
        raise InvalidRequestError("RitS pipeline input must have complete unique atom mapping")
    elements_by_map = {int(atom.GetAtomMapNum()): atom.GetAtomicNum() for atom in reactant_atoms}
    if any(
        elements_by_map[int(atom.GetAtomMapNum())] != atom.GetAtomicNum() for atom in product_atoms
    ):
        raise InvalidRequestError("RitS pipeline reactant/product elements differ by atom map")
    map_order = sorted(elements_by_map)
    contiguous = {old: index for index, old in enumerate(map_order, start=1)}

    def renumber(molecule: Any) -> str:
        copy = Chem.Mol(molecule)
        for atom in copy.GetAtoms():
            atom.SetAtomMapNum(contiguous[int(atom.GetAtomMapNum())])
        return Chem.MolToSmiles(copy, canonical=False, allHsExplicit=True)

    return f"{renumber(reactants)}>>{renumber(products)}"


@dataclass(frozen=True, slots=True)
class RitsPipelineConfig:
    """Configuration for the fork-maintained ``run_ts_pipeline.py`` workflow."""

    enabled: bool = True
    script: str = DEFAULT_RITS_PIPELINE_SCRIPT
    reaction_file: str = "inputs/reaction.smiles"
    candidates_dir: str = "outputs/rits-candidates"
    output_dir: str = "outputs/reaction-ts-pipeline"
    generate: bool = True
    generation_strategy: str = "hybrid"
    max_atoms_per_batch: int | None = 18000
    cluster_threshold: float = 0.5
    cluster_atoms: str = "heavy"
    stereo_filter: bool = True
    energy_scope: str = "staged"
    energy_window_hartree: float = 0.01
    gxtb_binary: str | None = None
    energy_workers: int = 1
    omp_threads: int = 1
    energy_timeout: float = 300.0
    charge: int | None = None
    uhf: int | None = None

    def __post_init__(self) -> None:
        for name in ("script", "reaction_file", "candidates_dir", "output_dir"):
            _text(getattr(self, name), f"rits.pipeline.{name}")
        if self.generation_strategy not in {"hybrid", "global"}:
            raise InvalidRequestError("rits.pipeline.generation_strategy must be hybrid or global")
        if self.cluster_atoms not in {"heavy", "all"}:
            raise InvalidRequestError("rits.pipeline.cluster_atoms must be heavy or all")
        if self.energy_scope not in {"staged", "medoids", "all"}:
            raise InvalidRequestError("rits.pipeline.energy_scope must be staged, medoids, or all")
        if self.max_atoms_per_batch is not None and self.max_atoms_per_batch < 1:
            raise InvalidRequestError(
                "rits.pipeline.max_atoms_per_batch must be positive when provided"
            )
        if self.cluster_threshold < 0.0 or self.energy_window_hartree < 0.0:
            raise InvalidRequestError("rits.pipeline thresholds must be non-negative")
        if self.energy_workers < 1 or self.omp_threads < 1:
            raise InvalidRequestError("rits.pipeline worker counts must be positive")
        if self.energy_timeout <= 0.0:
            raise InvalidRequestError("rits.pipeline.energy_timeout must be positive")
        if self.gxtb_binary is not None and not str(self.gxtb_binary).strip():
            raise InvalidRequestError("rits.pipeline.gxtb_binary must not be empty")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> RitsPipelineConfig:
        values = dict(payload or {})
        allowed = {
            "enabled",
            "script",
            "reaction_file",
            "candidates_dir",
            "output_dir",
            "generate",
            "generation_strategy",
            "max_atoms_per_batch",
            "cluster_threshold",
            "cluster_atoms",
            "stereo_filter",
            "energy_scope",
            "energy_window_hartree",
            "gxtb_binary",
            "energy_workers",
            "omp_threads",
            "energy_timeout",
            "charge",
            "uhf",
        }
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise InvalidRequestError("unknown rits.pipeline fields: " + ", ".join(unknown))
        return cls(
            enabled=bool(values.get("enabled", True)),
            script=str(values.get("script", DEFAULT_RITS_PIPELINE_SCRIPT)),
            reaction_file=str(values.get("reaction_file", "inputs/reaction.smiles")),
            candidates_dir=str(values.get("candidates_dir", "outputs/rits-candidates")),
            output_dir=str(values.get("output_dir", "outputs/reaction-ts-pipeline")),
            generate=bool(values.get("generate", True)),
            generation_strategy=str(values.get("generation_strategy", "hybrid")),
            max_atoms_per_batch=(
                int(values.get("max_atoms_per_batch", 18000))
                if values.get("max_atoms_per_batch", 18000) is not None
                else None
            ),
            cluster_threshold=float(values.get("cluster_threshold", 0.5)),
            cluster_atoms=str(values.get("cluster_atoms", "heavy")),
            stereo_filter=bool(values.get("stereo_filter", True)),
            energy_scope=str(values.get("energy_scope", "staged")),
            energy_window_hartree=float(values.get("energy_window_hartree", 0.01)),
            gxtb_binary=(
                str(values["gxtb_binary"]) if values.get("gxtb_binary") is not None else None
            ),
            energy_workers=int(values.get("energy_workers", 1)),
            omp_threads=int(values.get("omp_threads", 1)),
            energy_timeout=float(values.get("energy_timeout", 300.0)),
            charge=int(values["charge"]) if values.get("charge") is not None else None,
            uhf=int(values["uhf"]) if values.get("uhf") is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "script": self.script,
            "reaction_file": self.reaction_file,
            "candidates_dir": self.candidates_dir,
            "output_dir": self.output_dir,
            "generate": self.generate,
            "generation_strategy": self.generation_strategy,
            "max_atoms_per_batch": self.max_atoms_per_batch,
            "cluster_threshold": self.cluster_threshold,
            "cluster_atoms": self.cluster_atoms,
            "stereo_filter": self.stereo_filter,
            "energy_scope": self.energy_scope,
            "energy_window_hartree": self.energy_window_hartree,
            "gxtb_binary": self.gxtb_binary,
            "energy_workers": self.energy_workers,
            "omp_threads": self.omp_threads,
            "energy_timeout": self.energy_timeout,
            "charge": self.charge,
            "uhf": self.uhf,
        }


@dataclass(frozen=True, slots=True)
class RitsInferenceConfig:
    """RitS inference settings for one mapped reaction.

    The code/checkpoint artifacts are optional at the planning layer so a
    local caller can inject a sampler without pretending to upload anything.
    ``to_bohr_job`` still records every artifact that was supplied and uses the
    RitS command builder.  The profile path uses the configured fork's
    candidate-generation/pipeline entrypoint by default; the raw
    sampler command remains available when ``pipeline.enabled`` is false.
    """

    model_name: str = "reactionopt-rits-ts"
    image: str = DEFAULT_RITS_IMAGE
    code_artifact: Artifact | None = None
    checkpoint_artifact: Artifact | None = field(
        default_factory=lambda: Artifact(
            "rits-checkpoint",
            DEFAULT_RITS_CHECKPOINT_SOURCE,
            kind="file",
        )
    )
    input_artifacts: tuple[Artifact, ...] = ()
    output_artifacts: tuple[Artifact, ...] = ()
    input_directory: str = "."
    project_id: str | None = None
    machine_type: str | None = None
    python_executable: str = "python3.12"
    script: str = DEFAULT_RITS_SCRIPT
    config: str = DEFAULT_RITS_CONFIG
    checkpoint: str = DEFAULT_RITS_CHECKPOINT
    n_samples: int = 100
    batch_size: int = 32
    charge: int = 0
    num_steps: int = 16
    device: str = "cuda:0"
    kekulize: bool = False
    add_stereo: bool = False
    extra_args: tuple[str, ...] = ()
    source_repository: str = "https://github.com/gentle1999/RitS"
    source_revision: str | None = "6d8aa0704c21d2798326a7e070491abf438ca292"
    pipeline: RitsPipelineConfig = field(default_factory=RitsPipelineConfig)

    def __post_init__(self) -> None:
        for name in ("model_name", "image", "input_directory", "python_executable", "script"):
            _text(getattr(self, name), f"rits.{name}")
        if self.n_samples < 1 or self.batch_size < 1 or self.num_steps < 1:
            raise InvalidRequestError("rits n_samples, batch_size, and num_steps must be positive")
        if not str(self.config).strip() or not str(self.checkpoint).strip():
            raise InvalidRequestError("rits.config and rits.checkpoint must not be empty")
        if self.source_revision is not None and not str(self.source_revision).strip():
            raise InvalidRequestError("rits.source_revision must not be empty when provided")
        if not isinstance(self.pipeline, RitsPipelineConfig):
            if not isinstance(self.pipeline, Mapping):
                raise InvalidRequestError("rits.pipeline must be a table")
            object.__setattr__(self, "pipeline", RitsPipelineConfig.from_mapping(self.pipeline))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> RitsInferenceConfig:
        values = dict(payload or {})
        code_value = values.get(
            "code_artifact",
            values.get("code", values.get("source_artifact")),
        )
        checkpoint_value = values.get("checkpoint_artifact", values.get("checkpoint"))
        input_artifacts = values.get("input_artifacts", ())
        if input_artifacts is None:
            input_artifacts = ()
        if not isinstance(input_artifacts, (list, tuple)):
            raise InvalidRequestError("rits.input_artifacts must be a list")
        parsed_inputs = tuple(
            artifact
            for index, item in enumerate(input_artifacts)
            if (artifact := _artifact(item, name=f"input-{index}")) is not None
        )
        allowed = {
            "model_name",
            "image",
            "input_directory",
            "input_dir",
            "project_id",
            "machine_type",
            "python_executable",
            "script",
            "config",
            "checkpoint_path",
            "n_samples",
            "batch_size",
            "charge",
            "num_steps",
            "device",
            "kekulize",
            "add_stereo",
            "extra_args",
            "source_repository",
            "source_revision",
            "pipeline",
        }
        unknown = sorted(
            set(values)
            - allowed
            - {"code_artifact", "code", "source_artifact"}
            - {
                "checkpoint_artifact",
                "checkpoint",
                "input_artifacts",
                "output_artifacts",
            }
        )
        if unknown:
            raise InvalidRequestError(f"unknown rits inference fields: {', '.join(unknown)}")
        checkpoint_location = DEFAULT_RITS_CHECKPOINT_SOURCE
        if isinstance(checkpoint_value, Mapping):
            checkpoint_location = str(checkpoint_value.get("location", checkpoint_location))
        elif checkpoint_value is not None:
            checkpoint_location = str(checkpoint_value)
        checkpoint_path = str(values.get("checkpoint_path", DEFAULT_RITS_CHECKPOINT))
        pipeline_value = values.get("pipeline", {})
        if pipeline_value is None:
            pipeline_value = {}
        if not isinstance(pipeline_value, Mapping):
            raise InvalidRequestError("rits.pipeline must be a table")
        return cls(
            model_name=str(values.get("model_name", "reactionopt-rits-ts")),
            image=str(values.get("image", DEFAULT_RITS_IMAGE)),
            code_artifact=_artifact(code_value, name="code", default_kind="directory"),
            checkpoint_artifact=_artifact(
                checkpoint_value,
                name="checkpoint",
                default_location=checkpoint_location,
            ),
            input_artifacts=parsed_inputs,
            output_artifacts=_artifact_list(values.get("output_artifacts")),
            input_directory=str(values.get("input_directory", values.get("input_dir", "."))),
            project_id=(
                str(values["project_id"]) if values.get("project_id") is not None else None
            ),
            machine_type=(
                str(values["machine_type"]) if values.get("machine_type") is not None else None
            ),
            python_executable=str(values.get("python_executable", "python3.12")),
            script=str(values.get("script", DEFAULT_RITS_SCRIPT)),
            config=str(values.get("config", DEFAULT_RITS_CONFIG)),
            checkpoint=checkpoint_path,
            n_samples=int(values.get("n_samples", 100)),
            batch_size=int(values.get("batch_size", 32)),
            charge=int(values.get("charge", 0)),
            num_steps=int(values.get("num_steps", 16)),
            device=str(values.get("device", "cuda:0")),
            kekulize=bool(values.get("kekulize", False)),
            add_stereo=bool(values.get("add_stereo", False)),
            extra_args=_extra_args(values.get("extra_args")),
            source_repository=str(
                values.get("source_repository", "https://github.com/gentle1999/RitS")
            ),
            source_revision=(
                str(values["source_revision"])
                if values.get("source_revision") is not None
                else None
            ),
            pipeline=RitsPipelineConfig.from_mapping(pipeline_value),
        )

    @property
    def all_input_artifacts(self) -> tuple[Artifact, ...]:
        required = tuple(
            artifact
            for artifact in (self.code_artifact, self.checkpoint_artifact)
            if artifact is not None
        )
        return required + self.input_artifacts

    def build_command(
        self,
        *,
        output: str,
        reaction_smiles: str | None = None,
        reactant_smiles: str | None = None,
        product_smiles: str | None = None,
    ) -> tuple[str, ...]:
        try:
            from tricycle_rits.commands import build_inference_command
        except ImportError as exc:  # pragma: no cover - dependency installation detail
            raise InvalidRequestError(
                "ReactionOpt RitS planning requires the tricycle-rits package"
            ) from exc
        resolved_reactants, resolved_products = _resolve_reaction_inputs(
            reaction_smiles=reaction_smiles,
            reactant_smiles=reactant_smiles,
            product_smiles=product_smiles,
        )
        return build_inference_command(
            project_dir=self.input_directory,
            python_executable=self.python_executable,
            script=self.script,
            config=self.config,
            checkpoint=self.checkpoint,
            output=output,
            reactant_smi=resolved_reactants,
            product_smi=resolved_products,
            n_samples=self.n_samples,
            batch_size=self.batch_size,
            charge=self.charge,
            num_steps=self.num_steps,
            kekulize=self.kekulize,
            add_stereo=self.add_stereo,
            device=self.device,
            extra_args=self.extra_args,
        )

    def build_pipeline_command(
        self,
        *,
        reactions: str | None = None,
        candidates: str | None = None,
        output: str | None = None,
    ) -> tuple[str, ...]:
        """Build the complete RitS candidate-generation and ranking command."""

        if not self.pipeline.enabled:
            raise InvalidRequestError("rits.pipeline.enabled is false")
        try:
            from tricycle_rits.commands import build_ts_pipeline_command
        except ImportError as exc:  # pragma: no cover - dependency installation detail
            raise InvalidRequestError(
                "ReactionOpt RitS planning requires the tricycle-rits package"
            ) from exc
        return build_ts_pipeline_command(
            project_dir=self.input_directory,
            python_executable=self.python_executable,
            script=self.pipeline.script,
            reactions=reactions or self.pipeline.reaction_file,
            config=self.config,
            checkpoint=self.checkpoint,
            candidates=candidates or self.pipeline.candidates_dir,
            output=output or self.pipeline.output_dir,
            generate=self.pipeline.generate,
            generation_strategy=self.pipeline.generation_strategy,
            n_samples=self.n_samples,
            num_steps=self.num_steps,
            device=self.device,
            batch_size=self.batch_size,
            max_atoms_per_batch=self.pipeline.max_atoms_per_batch,
            cluster_threshold=self.pipeline.cluster_threshold,
            cluster_atoms=self.pipeline.cluster_atoms,
            stereo_filter=self.pipeline.stereo_filter,
            energy_scope=self.pipeline.energy_scope,
            energy_window_hartree=self.pipeline.energy_window_hartree,
            gxtb_binary=self.pipeline.gxtb_binary,
            energy_workers=self.pipeline.energy_workers,
            omp_threads=self.pipeline.omp_threads,
            energy_timeout=self.pipeline.energy_timeout,
            charge=self.pipeline.charge,
            uhf=self.pipeline.uhf,
            extra_args=self.extra_args,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "image": self.image,
            "code_artifact": self.code_artifact.to_dict() if self.code_artifact else None,
            "checkpoint_artifact": (
                self.checkpoint_artifact.to_dict() if self.checkpoint_artifact else None
            ),
            "input_artifacts": [artifact.to_dict() for artifact in self.input_artifacts],
            "output_artifacts": [artifact.to_dict() for artifact in self.output_artifacts],
            "input_directory": self.input_directory,
            "project_id": self.project_id,
            "machine_type": self.machine_type,
            "python_executable": self.python_executable,
            "script": self.script,
            "config": self.config,
            "checkpoint": self.checkpoint,
            "n_samples": self.n_samples,
            "batch_size": self.batch_size,
            "charge": self.charge,
            "num_steps": self.num_steps,
            "device": self.device,
            "kekulize": self.kekulize,
            "add_stereo": self.add_stereo,
            "extra_args": list(self.extra_args),
            "source_repository": self.source_repository,
            "source_revision": self.source_revision,
            "pipeline": self.pipeline.to_dict(),
        }

    def to_bohr_job(
        self,
        *,
        job_name: str,
        output: str,
        reaction_smiles: str | None = None,
        reactant_smiles: str | None = None,
        product_smiles: str | None = None,
    ) -> BohrJobSpec:
        resolved_reactants, resolved_products = _resolve_reaction_inputs(
            reaction_smiles=reaction_smiles,
            reactant_smiles=reactant_smiles,
            product_smiles=product_smiles,
        )
        if self.pipeline.enabled:
            pipeline_reaction = _pipeline_reaction_smiles(resolved_reactants, resolved_products)
            output_artifacts = self.output_artifacts or (
                Artifact(
                    "rits-ts-pipeline-output",
                    output,
                    kind="directory",
                    media_type="application/x-rits-ts-pipeline",
                ),
            )
            command = self.build_pipeline_command(output=output)
        else:
            output_artifacts = self.output_artifacts or (
                Artifact("rits-ts-output", output, kind="file", media_type="chemical/x-xyz"),
            )
            command = self.build_command(
                output=output,
                reactant_smiles=resolved_reactants,
                product_smiles=resolved_products,
            )
        parameters = {
            "input_directory": self.input_directory,
            "model_name": self.model_name,
            "mode": "infer",
            "reaction_input_mode": "mapped-smiles",
            "reactant_smiles": resolved_reactants,
            "product_smiles": resolved_products,
            "reactionopt_ts_builder": "rits",
            "upstream_entrypoint": (self.pipeline.script if self.pipeline.enabled else self.script),
            "upstream_protocol": (
                "run_ts_pipeline" if self.pipeline.enabled else "sample_transition_state"
            ),
            "source_repository": self.source_repository,
            "source_revision": self.source_revision,
            "ts_pipeline": self.pipeline.to_dict(),
        }
        if self.pipeline.enabled:
            parameters.update(
                {
                    "pipeline_reaction_smiles": pipeline_reaction,
                    "generated_input_files": {
                        self.pipeline.reaction_file: pipeline_reaction + "\n",
                    },
                }
            )
        return BohrJobSpec(
            name=job_name,
            operation="rits.infer",
            image=self.image,
            command=command,
            project_id=self.project_id,
            machine_type=self.machine_type,
            input_artifacts=self.all_input_artifacts,
            output_artifacts=output_artifacts,
            parameters=parameters,
            tags=("tricycle", "reactionopt", "rits", "ts"),
        )


@dataclass(frozen=True, slots=True)
class RitsTransitionStateInput:
    """One mapped reaction handed to the injected RitS sampler.

    RitS generates transition-state coordinates from mapped reactant/product
    connectivity, so this boundary deliberately contains SMILES and
    configuration only.
    """

    reaction_smiles: str
    config: RitsInferenceConfig
    reactant_smiles: str | None = None
    product_smiles: str | None = None

    def __post_init__(self) -> None:
        reaction_smiles = _text(self.reaction_smiles, "reaction_smiles")
        if self.reactant_smiles is None and self.product_smiles is None:
            reactants, products = _split_reaction_smiles(reaction_smiles)
        elif self.reactant_smiles is None or self.product_smiles is None:
            raise InvalidRequestError(
                "reactant_smiles and product_smiles must be provided together"
            )
        else:
            reactants = _text(self.reactant_smiles, "reactant_smiles")
            products = _text(self.product_smiles, "product_smiles")
        object.__setattr__(self, "reaction_smiles", reaction_smiles)
        object.__setattr__(self, "reactant_smiles", reactants)
        object.__setattr__(self, "product_smiles", products)


@dataclass(frozen=True, slots=True)
class RitsTransitionStateResult:
    """Normalized result returned by a RitS adapter.

    ``structures`` contains RDKit molecules with one conformer each.  The
    adapter may be backed by Bohr, a local RitS checkout, or a test double; the
    ReactionOpt layer does not assume a transport or credential mechanism.
    """

    structures: tuple[Any, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.structures, (str, bytes, Path)):
            raise InvalidRequestError(
                "RitS structures must be RDKit molecules; use from_xyz for an XYZ path"
            )
        structures = tuple(self.structures)
        if not structures:
            raise InvalidRequestError("RitS returned no transition-state structures")
        if not isinstance(self.metadata, Mapping):
            raise InvalidRequestError("RitS result metadata must be a mapping")
        object.__setattr__(self, "structures", structures)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @classmethod
    def from_xyz(
        cls,
        source: str | Path,
        template: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> RitsTransitionStateResult:
        """Create a result from the multi-block XYZ emitted by RitS."""

        return cls(parse_xyz_conformers(source, template), metadata or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "structure_count": len(self.structures),
            "metadata": dict(self.metadata),
        }


class RitsTransitionStateSampler(Protocol):
    """External boundary for the complete RitS TS pipeline.

    A conforming adapter must return the structures selected by
    ``run_ts_pipeline.py`` (stereo filtering, RMSD clustering, and g-xTB
    ranking), rather than treating raw ``sample_transition_state.py`` output
    as the final TS pool.
    """

    def sample(self, request: RitsTransitionStateInput) -> RitsTransitionStateResult: ...


RitsInferenceAdapter = RitsTransitionStateSampler


def _mapped_template_from_smiles(smiles: str) -> Any:
    Chem = _chem()
    params = Chem.SmilesParserParams()
    params.removeHs = False
    molecule = Chem.MolFromSmiles(smiles, params)
    if molecule is None:
        raise InvalidRequestError("could not parse the mapped reactant template")
    maps = [int(atom.GetAtomMapNum()) for atom in molecule.GetAtoms()]
    if any(number <= 0 for number in maps) or len(set(maps)) != len(maps):
        raise InvalidRequestError("RitS output restoration requires unique positive atom maps")
    order = [index for index, _ in sorted(enumerate(maps), key=lambda item: item[1])]
    return Chem.RenumberAtoms(molecule, order)


class LocalRitsPipelineSampler:
    """Run the configured RitS fork's complete TS pipeline in a local checkout.

    This is an explicit adapter, not the default execution path.  It invokes
    ``scripts/run_ts_pipeline.py --generate`` for one reaction, so the RitS
    model, stereo filter, clustering, and g-xTB ranking all run in the same
    environment.  A Bohr-backed adapter can implement the same protocol and
    reuse :meth:`RitsInferenceConfig.build_pipeline_command` instead.
    """

    def __init__(
        self,
        *,
        project_dir: str | Path | None = None,
        work_root: str | Path | None = None,
        keep_workdir: bool = False,
    ) -> None:
        self.project_dir = Path(project_dir).expanduser() if project_dir is not None else None
        self.work_root = Path(work_root).expanduser() if work_root is not None else None
        self.keep_workdir = bool(keep_workdir)

    def sample(self, request: RitsTransitionStateInput) -> RitsTransitionStateResult:
        config = request.config
        if not config.pipeline.enabled:
            raise InvalidRequestError(
                "LocalRitsPipelineSampler requires rits.pipeline.enabled=true"
            )
        project_dir = (self.project_dir or Path(config.input_directory)).resolve()
        if not project_dir.is_dir():
            raise InvalidRequestError(f"RitS project directory does not exist: {project_dir}")

        work_parent = self.work_root.resolve() if self.work_root is not None else None
        if work_parent is not None:
            work_parent.mkdir(parents=True, exist_ok=True)
        run_dir = Path(
            tempfile.mkdtemp(
                prefix="tricycle-rits-pipeline-",
                dir=str(work_parent) if work_parent is not None else None,
            )
        )
        try:
            reaction_file = run_dir / "reaction.smiles"
            candidates_dir = run_dir / "candidates"
            output_dir = run_dir / "pipeline-output"
            pipeline_reaction = _pipeline_reaction_smiles(
                request.reactant_smiles or "",
                request.product_smiles or "",
            )
            reaction_file.write_text(pipeline_reaction + "\n", encoding="utf-8")
            command = config.build_pipeline_command(
                reactions=str(reaction_file),
                candidates=str(candidates_dir),
                output=str(output_dir),
            )
            try:
                completed = subprocess.run(
                    command,
                    cwd=project_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError as exc:
                raise InvalidRequestError(f"could not start RitS TS pipeline: {exc}") from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()[-2400:]
                raise InvalidRequestError(
                    f"RitS TS pipeline failed with exit code {completed.returncode}: {detail}"
                )

            selected_dir = output_dir / "selected_xyz"
            selected_files = tuple(sorted(selected_dir.glob("*.xyz")))
            if not selected_files:
                summary_path = output_dir / "summary.json"
                summary = summary_path.read_text(encoding="utf-8") if summary_path.is_file() else ""
                raise InvalidRequestError(
                    "RitS TS pipeline returned no selected transition-state structures"
                    + (f": {summary[-1200:]}" if summary else "")
                )
            template = _mapped_template_from_smiles(request.reactant_smiles or "")
            structures: list[Any] = []
            for selected_file in selected_files:
                structures.extend(parse_xyz_conformers(selected_file, template))

            summary: Any = {}
            summary_path = output_dir / "summary.json"
            if summary_path.is_file():
                try:
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    summary = {"summary_parse_error": str(summary_path)}
            metadata = {
                "adapter": "local-rits-pipeline",
                "pipeline_script": config.pipeline.script,
                "pipeline_reaction_smiles": pipeline_reaction,
                "command": list(command),
                "selected_xyz_files": [path.name for path in selected_files],
                "summary": summary,
            }
            if self.keep_workdir:
                metadata["workdir"] = str(run_dir)
            return RitsTransitionStateResult(tuple(structures), metadata)
        finally:
            if not self.keep_workdir:
                shutil.rmtree(run_dir, ignore_errors=True)


def molecule_to_xyz(mol: Any, *, comment: str = "") -> str:
    """Serialize a one-conformer RDKit molecule in the format consumed by RitS."""

    if mol is None:
        raise InvalidRequestError("cannot serialize a missing molecule")
    if mol.GetNumConformers() != 1:
        raise InvalidRequestError("RitS XYZ input must contain exactly one conformer")
    conf = mol.GetConformer(0)
    lines = [str(mol.GetNumAtoms()), comment]
    for atom_index, atom in enumerate(mol.GetAtoms()):
        position = conf.GetAtomPosition(atom_index)
        lines.append(
            f"{atom.GetSymbol()} {float(position.x):.8f} "
            f"{float(position.y):.8f} {float(position.z):.8f}"
        )
    return "\n".join(lines) + "\n"


def write_xyz(mol: Any, destination: str | Path, *, comment: str = "") -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(molecule_to_xyz(mol, comment=comment), encoding="utf-8")
    return path


def parse_xyz_conformers(source: str | Path, template: Any) -> tuple[Any, ...]:
    """Parse one or more RitS XYZ blocks and restore mapped RDKit topology."""

    Chem = _chem()
    if isinstance(source, Path) or (isinstance(source, str) and "\n" not in source):
        candidate = Path(source)
        text = candidate.read_text(encoding="utf-8") if candidate.is_file() else str(source)
    else:
        text = str(source)
    blocks = _xyz_blocks(text)
    if not blocks:
        raise InvalidRequestError("RitS output contains no XYZ structure")
    expected_symbols = tuple(atom.GetSymbol() for atom in template.GetAtoms())
    structures: list[Any] = []
    for block_index, (symbols, coordinates) in enumerate(blocks):
        if tuple(symbols) != expected_symbols:
            raise InvalidRequestError(
                "RitS XYZ atom order/elements do not match the mapped reaction template "
                f"in block {block_index}"
            )
        structure = Chem.Mol(template)
        structure.RemoveAllConformers()
        conformer = Chem.Conformer(structure.GetNumAtoms())
        for atom_index, coordinate in enumerate(coordinates):
            conformer.SetAtomPosition(atom_index, coordinate)
        conformer.SetProp("TSBuilder", "rits")
        conformer.SetIntProp("RitsSampleIndex", block_index)
        conformer.SetBoolProp("Converged", False)
        structure.AddConformer(conformer, assignId=True)
        structures.append(structure)
    return tuple(structures)


def normalize_transition_state_structures(
    structures: Sequence[Any], template: Any
) -> tuple[Any, ...]:
    """Validate adapter-returned structures and preserve mapped reaction topology."""

    Chem = _chem()
    expected_symbols = tuple(atom.GetSymbol() for atom in template.GetAtoms())
    normalized: list[Any] = []
    structure_index = 0
    for structure in structures:
        if isinstance(structure, (str, Path)):
            parsed = parse_xyz_conformers(structure, template)
            for item in parsed:
                result = Chem.Mol(template)
                result.RemoveAllConformers()
                conformer = Chem.Conformer(item.GetConformer(0))
                conformer.SetProp("TSBuilder", "rits")
                conformer.SetIntProp("RitsSampleIndex", structure_index)
                result.AddConformer(conformer, assignId=True)
                normalized.append(result)
                structure_index += 1
            continue
        index = structure_index
        structure_index += 1
        if structure is None or structure.GetNumAtoms() != template.GetNumAtoms():
            raise InvalidRequestError(
                f"RitS structure {index} does not match the reaction atom count"
            )
        symbols = tuple(atom.GetSymbol() for atom in structure.GetAtoms())
        if symbols != expected_symbols:
            raise InvalidRequestError(f"RitS structure {index} does not preserve atom order")
        if structure.GetNumConformers() != 1:
            raise InvalidRequestError(f"RitS structure {index} must contain exactly one conformer")
        result = Chem.Mol(template)
        result.RemoveAllConformers()
        conformer = Chem.Conformer(structure.GetConformer(0))
        conformer.SetProp("TSBuilder", "rits")
        conformer.SetIntProp("RitsSampleIndex", index)
        result.AddConformer(conformer, assignId=True)
        normalized.append(result)
    if not normalized:
        raise InvalidRequestError("RitS returned no transition-state structures")
    return tuple(normalized)


def coerce_sampler_result(value: Any) -> RitsTransitionStateResult:
    if isinstance(value, RitsTransitionStateResult):
        return value
    if isinstance(value, (list, tuple)):
        return RitsTransitionStateResult(tuple(value))
    raise InvalidRequestError(
        "RitS sampler must return RitsTransitionStateResult or a sequence of RDKit molecules"
    )


def _chem() -> Any:
    try:
        from rdkit import Chem
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise InvalidRequestError(
            "ReactionOpt RitS structure handling requires the 'rdkit' package"
        ) from exc
    return Chem


def _xyz_blocks(text: str) -> list[tuple[list[str], list[tuple[float, float, float]]]]:
    lines = [line.strip() for line in text.splitlines()]
    blocks: list[tuple[list[str], list[tuple[float, float, float]]]] = []
    index = 0
    while index < len(lines):
        if not lines[index]:
            index += 1
            continue
        try:
            atom_count = int(lines[index])
        except ValueError as exc:
            raise InvalidRequestError("invalid XYZ atom count in RitS output") from exc
        if atom_count < 1 or index + atom_count + 1 >= len(lines):
            raise InvalidRequestError("truncated XYZ block in RitS output")
        atom_lines = lines[index + 2 : index + 2 + atom_count]
        if any(not line for line in atom_lines):
            raise InvalidRequestError("empty atom line in RitS XYZ output")
        symbols: list[str] = []
        coordinates: list[tuple[float, float, float]] = []
        for atom_line in atom_lines:
            fields = atom_line.split()
            if len(fields) < 4:
                raise InvalidRequestError("invalid atom line in RitS XYZ output")
            try:
                coordinate = (float(fields[1]), float(fields[2]), float(fields[3]))
            except ValueError as exc:
                raise InvalidRequestError("invalid coordinate in RitS XYZ output") from exc
            symbols.append(fields[0])
            coordinates.append(coordinate)
        blocks.append((symbols, coordinates))
        index += atom_count + 2
    return blocks


__all__ = [
    "DEFAULT_RITS_CHECKPOINT",
    "DEFAULT_RITS_CHECKPOINT_SOURCE",
    "DEFAULT_RITS_CONFIG",
    "DEFAULT_RITS_IMAGE",
    "DEFAULT_RITS_PIPELINE_SCRIPT",
    "DEFAULT_RITS_SCRIPT",
    "LocalRitsPipelineSampler",
    "RitsInferenceAdapter",
    "RitsInferenceConfig",
    "RitsPipelineConfig",
    "RitsTransitionStateInput",
    "RitsTransitionStateResult",
    "RitsTransitionStateSampler",
    "coerce_sampler_result",
    "molecule_to_xyz",
    "normalize_transition_state_structures",
    "parse_xyz_conformers",
    "write_xyz",
]
