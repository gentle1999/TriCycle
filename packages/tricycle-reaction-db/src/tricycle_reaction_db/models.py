from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from tricycle_core import Artifact, ReactionRecord
from tricycle_core.errors import InvalidRequestError

from .contracts import RITS_EXPORT_API_CONTRACT, RXNRESID_EXPORT_API_CONTRACT

_PROJECT_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# The database's reaction-structure predicate for the cycloaddition reaction
# space.  Keep this value explicit in the request so the adapter forwards the
# exact predicate instead of silently falling back to a reaction-class label.
CYCLOADDITION_RXN_SMARTS = (
    "[C,N]=[C,N].[C,N]=[C,N]-[C,N]=[C,N]>>"
    "[C,N]1[C,N][C,N][C,N]=[C,N][C,N]1"
)


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidRequestError(f"{field_name} must not be empty")
    return text


def _required_path(value: object, field_name: str) -> Path:
    text = _required_text(value, field_name)
    return Path(text)


def _required_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidRequestError(f"{field_name} must be a boolean")
    return value


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{field_name} must be an object")
    return value


def _mapping_items(value: object, field_name: str) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise InvalidRequestError(f"{field_name} must be a list")
    items: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise InvalidRequestError(f"{field_name}[{index}] must be an object")
        items.append(item)
    return tuple(items)


def _validate_project_slug(slug: str) -> None:
    if not _PROJECT_SLUG.fullmatch(slug):
        raise InvalidRequestError(
            "project slug must contain only lowercase letters, digits, and single hyphens"
        )


def _validate_relative_directory(path: Path, field_name: str) -> None:
    if path.is_absolute() or ".." in path.parts:
        raise InvalidRequestError(f"{field_name} must stay inside the batch root")


@dataclass(frozen=True, slots=True)
class ReactionDatabaseProjectSpec:
    """Creation metadata for one internal database project.

    The adapter creates the internal project and returns its authoritative
    ``project_id``.  The package intentionally does not accept a project ID in
    a new-import request, so an import cannot silently append to an existing
    project.
    """

    slug: str
    name: str
    organization_id: str | None = None

    def __post_init__(self) -> None:
        slug = _required_text(self.slug, "project.slug")
        name = _required_text(self.name, "project.name")
        _validate_project_slug(slug)
        object.__setattr__(self, "slug", slug)
        object.__setattr__(self, "name", name)
        if self.organization_id is not None:
            object.__setattr__(
                self,
                "organization_id",
                _required_text(self.organization_id, "project.organization_id"),
            )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ReactionDatabaseProjectSpec:
        return cls(
            slug=str(payload.get("slug", payload.get("project_slug", ""))),
            name=str(payload.get("name", payload.get("project_name", ""))),
            organization_id=payload.get("organization_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "organization_id": self.organization_id,
        }


@dataclass(frozen=True, slots=True)
class CalculationLogBatchSpec:
    """A Bohr batch directory and the exact log-only input boundary.

    ``logs_dir`` is relative to ``root`` and every ``*.log`` below it is
    selected recursively.  This makes the request incapable of accidentally
    uploading Gaussian inputs, JSON metadata, worker ``.out`` files, or
    arbitrary files from a sibling batch.
    """

    batch_id: str
    root: Path
    logs_dir: Path = Path("downloads")
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "batch_id", _required_text(self.batch_id, "batch.batch_id"))
        if not isinstance(self.root, Path):
            object.__setattr__(self, "root", Path(str(self.root)))
        if not isinstance(self.logs_dir, Path):
            object.__setattr__(self, "logs_dir", Path(str(self.logs_dir)))
        _validate_relative_directory(self.logs_dir, "batch.logs_dir")
        object.__setattr__(self, "metadata", dict(_mapping(self.metadata, "batch.metadata")))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> CalculationLogBatchSpec:
        return cls(
            batch_id=str(payload.get("batch_id", payload.get("id", ""))),
            root=_required_path(payload.get("root", payload.get("path", "")), "batch.root"),
            logs_dir=Path(str(payload.get("logs_dir", "downloads"))),
            metadata=dict(_mapping(payload.get("metadata", {}), "batch.metadata")),
        )

    def resolve_root(self, base_dir: Path = Path(".")) -> Path:
        candidate = self.root if self.root.is_absolute() else base_dir / self.root
        root = candidate.expanduser().resolve()
        if not root.is_dir():
            raise InvalidRequestError(f"batch root does not exist or is not a directory: {root}")
        return root

    def discover(self, base_dir: Path = Path(".")) -> CalculationLogBatchManifest:
        root = self.resolve_root(base_dir)
        source_dir = (root / self.logs_dir).resolve()
        try:
            source_dir.relative_to(root)
        except ValueError as exc:
            raise InvalidRequestError("batch.logs_dir must stay inside batch.root") from exc
        if not source_dir.is_dir():
            raise InvalidRequestError(f"batch logs directory does not exist: {source_dir}")

        candidates = sorted(
            source_dir.rglob("*.log"),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        if not candidates:
            raise InvalidRequestError(f"batch contains no calculation logs: {source_dir}")

        files: list[Path] = []
        for candidate in candidates:
            if candidate.is_symlink():
                raise InvalidRequestError(f"calculation logs must not be symlinks: {candidate}")
            try:
                resolved = candidate.resolve(strict=True)
            except FileNotFoundError as exc:
                raise InvalidRequestError(f"calculation log disappeared: {candidate}") from exc
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise InvalidRequestError(f"calculation log escapes batch root: {candidate}") from exc
            if not resolved.is_file():
                raise InvalidRequestError(f"calculation log is not a regular file: {candidate}")
            files.append(resolved)

        return CalculationLogBatchManifest.from_files(
            batch=self,
            root=root,
            source_dir=source_dir,
            files=tuple(files),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "root": str(self.root),
            "logs_dir": self.logs_dir.as_posix(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CalculationLogBatchManifest:
    """Resolved, auditable file set passed to a database adapter."""

    batch_id: str
    root: Path
    source_dir: Path
    files: tuple[Path, ...]
    total_bytes: int
    manifest_digest: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_files(
        cls,
        *,
        batch: CalculationLogBatchSpec,
        root: Path,
        source_dir: Path,
        files: tuple[Path, ...],
    ) -> CalculationLogBatchManifest:
        digest = sha256()
        total_bytes = 0
        for path in files:
            relative = path.relative_to(root).as_posix()
            size = path.stat().st_size
            total_bytes += size
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(size).encode("ascii"))
            digest.update(b"\n")
        return cls(
            batch_id=batch.batch_id,
            root=root,
            source_dir=source_dir,
            files=files,
            total_bytes=total_bytes,
            manifest_digest=digest.hexdigest(),
            metadata=dict(batch.metadata),
        )

    def to_dict(self, *, include_files: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "batch_id": self.batch_id,
            "root": str(self.root),
            "source_dir": str(self.source_dir),
            "log_count": len(self.files),
            "total_bytes": self.total_bytes,
            "manifest_digest": self.manifest_digest,
            "metadata": dict(self.metadata),
        }
        if include_files:
            result["files"] = [str(path) for path in self.files]
        else:
            result["sample_files"] = [
                str(path.relative_to(self.root)) for path in self.files[:3]
            ]
        return result


@dataclass(frozen=True, slots=True)
class ReactionBatchImportRequest:
    """Create one internal database project from exactly one calculation batch."""

    project: ReactionDatabaseProjectSpec
    batch: CalculationLogBatchSpec
    source: str = "bohr"
    provenance: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.batch, CalculationLogBatchSpec):
            raise InvalidRequestError("batch must be a CalculationLogBatchSpec value")
        source = _required_text(self.source, "source")
        provenance = dict(_mapping(self.provenance, "provenance"))
        for key in ("workflow", "initial_guess_source"):
            if not _required_text(provenance.get(key), f"provenance.{key}"):
                raise InvalidRequestError(f"provenance.{key} must not be empty")
        if source.casefold().startswith("bohr"):
            for key in ("bohr_job_group_id", "bohr_project_id"):
                if not _required_text(self.batch.metadata.get(key), f"batch.metadata.{key}"):
                    raise InvalidRequestError(
                        f"Bohr imports require batch.metadata.{key} for {self.batch.batch_id}"
                    )
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "metadata", dict(_mapping(self.metadata, "metadata")))
        if self.idempotency_key is not None:
            object.__setattr__(
                self,
                "idempotency_key",
                _required_text(self.idempotency_key, "idempotency_key"),
            )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ReactionBatchImportRequest:
        project_payload = payload.get("project")
        if project_payload is None:
            project_payload = {
                "slug": payload.get("project_slug", ""),
                "name": payload.get("project_name", ""),
                "organization_id": payload.get("organization_id"),
            }
        if not isinstance(project_payload, Mapping):
            raise InvalidRequestError("project must be an object")
        if "project_id" in payload or "project_id" in project_payload:
            raise InvalidRequestError(
                "batch import creates a new internal project; project_id is not accepted"
            )
        raw_batch = payload.get("batch")
        if raw_batch is None:
            raw_batches = _mapping_items(payload.get("batches"), "batches")
            if len(raw_batches) != 1:
                raise InvalidRequestError(
                    "one import request must contain exactly one batch; create one project per batch"
                )
            raw_batch = raw_batches[0]
        if not isinstance(raw_batch, Mapping):
            raise InvalidRequestError("batch must be an object")
        return cls(
            project=ReactionDatabaseProjectSpec.from_mapping(project_payload),
            batch=CalculationLogBatchSpec.from_mapping(raw_batch),
            source=str(payload.get("source", "bohr")),
            provenance=dict(_mapping(payload.get("provenance", {}), "provenance")),
            idempotency_key=payload.get("idempotency_key"),
            metadata=dict(_mapping(payload.get("metadata", {}), "metadata")),
        )

    def resolve_batch(self, base_dir: Path = Path(".")) -> CalculationLogBatchManifest:
        return self.batch.discover(base_dir)

    def resolved_idempotency_key(self, manifest: CalculationLogBatchManifest) -> str:
        if self.idempotency_key is not None:
            return self.idempotency_key
        digest = sha256()
        digest.update(self.project.slug.encode("utf-8"))
        digest.update(manifest.batch_id.encode("utf-8"))
        digest.update(manifest.manifest_digest.encode("ascii"))
        return f"{self.project.slug}:{digest.hexdigest()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project.to_dict(),
            "batch": self.batch.to_dict(),
            "source": self.source,
            "provenance": dict(self.provenance),
            "idempotency_key": self.idempotency_key,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ReactionFilterRequest:
    """Select cleaned cycloaddition reactions inside one database project.

    The request carries the exact database rxn-SMARTS predicate.  The injected
    adapter forwards it to the database; TriCycle does not reimplement reaction
    classification locally or use the optional ``reaction_class`` field.
    """

    project_id: str
    reaction_filter: str = "cycloaddition"
    cleaned_only: bool = True
    reaction_smarts: str = CYCLOADDITION_RXN_SMARTS

    def __post_init__(self) -> None:
        _validate_project_id(self.project_id)
        _validate_database_filter(
            self.reaction_filter,
            self.cleaned_only,
            self.reaction_smarts,
        )
        object.__setattr__(self, "project_id", str(self.project_id).strip())
        object.__setattr__(self, "reaction_filter", "cycloaddition")
        object.__setattr__(self, "reaction_smarts", CYCLOADDITION_RXN_SMARTS)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ReactionFilterRequest:
        _reject_reaction_class(payload)
        return cls(
            project_id=str(payload.get("project_id", "")),
            reaction_filter=str(
                payload.get("reaction_filter", payload.get("filter", "cycloaddition"))
            ),
            cleaned_only=_required_bool(payload.get("cleaned_only", True), "cleaned_only"),
            reaction_smarts=str(
                payload.get(
                    "reaction_smarts",
                    payload.get("rxn_smarts", CYCLOADDITION_RXN_SMARTS),
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "reaction_filter": self.reaction_filter,
            "cleaned_only": self.cleaned_only,
            "reaction_smarts": self.reaction_smarts,
        }


def _validate_project_id(project_id: object) -> str:
    return _required_text(project_id, "project_id")


def _validate_database_filter(
    reaction_filter: object,
    cleaned_only: bool,
    reaction_smarts: object,
) -> None:
    if _required_text(reaction_filter, "reaction_filter").casefold() != "cycloaddition":
        raise InvalidRequestError(
            "reaction database operations must use the authoritative cycloaddition rxn-SMARTS filter"
        )
    if _required_bool(cleaned_only, "cleaned_only") is not True:
        raise InvalidRequestError("reaction database operations require cleaned_only=true")
    if _required_text(reaction_smarts, "reaction_smarts") != CYCLOADDITION_RXN_SMARTS:
        raise InvalidRequestError(
            "reaction_smarts must be the configured cycloaddition rxn-SMARTS predicate"
        )


def _reject_reaction_class(payload: Mapping[str, Any]) -> None:
    if "reaction_class" in payload:
        raise InvalidRequestError(
            "reaction_class is not supported; use the configured reaction_smarts predicate"
        )


@dataclass(frozen=True, slots=True)
class RitsDatasetExportRequest:
    """Export mapped reaction SMILES and transition-state structures for RitS."""

    project_id: str
    destination: Path = Path("reaction-space-rits.csv")
    reaction_filter: str = "cycloaddition"
    cleaned_only: bool = True
    reaction_smarts: str = CYCLOADDITION_RXN_SMARTS
    structure_format: str = "xyz"
    consumer: str = "RitS"

    def __post_init__(self) -> None:
        project_id = _validate_project_id(self.project_id)
        _validate_database_filter(
            self.reaction_filter,
            self.cleaned_only,
            self.reaction_smarts,
        )
        destination = self.destination
        if not isinstance(destination, Path):
            destination = Path(str(destination))
        if destination.suffix.casefold() != ".csv":
            raise InvalidRequestError("RitS dataset destination must end in .csv")
        if _required_text(self.consumer, "consumer").casefold() != "rits":
            raise InvalidRequestError("this dataset export consumer must be RitS")
        structure_format = _required_text(self.structure_format, "structure_format").casefold()
        if structure_format not in {"xyz", "sdf", "database-reference"}:
            raise InvalidRequestError(
                "structure_format must be xyz, sdf, or database-reference"
            )
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "reaction_filter", "cycloaddition")
        object.__setattr__(self, "reaction_smarts", CYCLOADDITION_RXN_SMARTS)
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "structure_format", structure_format)
        object.__setattr__(self, "consumer", "RitS")

    @property
    def api_contract(self) -> str:
        """The standard mapped-reaction/geometry API contract for this export."""

        return RITS_EXPORT_API_CONTRACT

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> RitsDatasetExportRequest:
        _reject_reaction_class(payload)
        return cls(
            project_id=str(payload.get("project_id", "")),
            destination=_required_path(
                payload.get("destination", payload.get("output", "")),
                "destination",
            ),
            reaction_filter=str(
                payload.get("reaction_filter", payload.get("filter", "cycloaddition"))
            ),
            cleaned_only=_required_bool(payload.get("cleaned_only", True), "cleaned_only"),
            reaction_smarts=str(
                payload.get(
                    "reaction_smarts",
                    payload.get("rxn_smarts", CYCLOADDITION_RXN_SMARTS),
                )
            ),
            structure_format=str(payload.get("structure_format", "xyz")),
            consumer=str(payload.get("consumer", "RitS")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "reaction_filter": self.reaction_filter,
            "cleaned_only": self.cleaned_only,
            "reaction_smarts": self.reaction_smarts,
            "destination": str(self.destination),
            "consumer": self.consumer,
            "structure_format": self.structure_format,
            "api_contract": self.api_contract,
            "required_columns": ["mapped_reaction_smiles", "ts_structure"],
        }


@dataclass(frozen=True, slots=True)
class RxnResidDatasetExportRequest:
    """Export energetic and phase-resolved timing rows for RxnResid."""

    project_id: str
    destination: Path = Path("reaction-space-rxnresid.csv")
    reaction_filter: str = "cycloaddition"
    cleaned_only: bool = True
    reaction_smarts: str = CYCLOADDITION_RXN_SMARTS
    consumer: str = "RxnResid"
    energy_unit: str = "database-native"
    timing_unit: str = "seconds"

    def __post_init__(self) -> None:
        project_id = _validate_project_id(self.project_id)
        _validate_database_filter(
            self.reaction_filter,
            self.cleaned_only,
            self.reaction_smarts,
        )
        destination = self.destination
        if not isinstance(destination, Path):
            destination = Path(str(destination))
        if destination.suffix.casefold() != ".csv":
            raise InvalidRequestError("RxnResid dataset destination must end in .csv")
        if _required_text(self.consumer, "consumer").casefold() != "rxnresid":
            raise InvalidRequestError("this dataset export consumer must be RxnResid")
        energy_unit = _required_text(self.energy_unit, "energy_unit")
        if _required_text(self.timing_unit, "timing_unit").casefold() != "seconds":
            raise InvalidRequestError("timing_unit must be seconds")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "reaction_filter", "cycloaddition")
        object.__setattr__(self, "reaction_smarts", CYCLOADDITION_RXN_SMARTS)
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "consumer", "RxnResid")
        object.__setattr__(self, "energy_unit", energy_unit)
        object.__setattr__(self, "timing_unit", "seconds")

    @property
    def api_contract(self) -> str:
        """The standard database thermodynamics CSV API contract."""

        return RXNRESID_EXPORT_API_CONTRACT

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> RxnResidDatasetExportRequest:
        _reject_reaction_class(payload)
        return cls(
            project_id=str(payload.get("project_id", "")),
            destination=_required_path(
                payload.get("destination", payload.get("output", "")),
                "destination",
            ),
            reaction_filter=str(
                payload.get("reaction_filter", payload.get("filter", "cycloaddition"))
            ),
            cleaned_only=_required_bool(payload.get("cleaned_only", True), "cleaned_only"),
            reaction_smarts=str(
                payload.get(
                    "reaction_smarts",
                    payload.get("rxn_smarts", CYCLOADDITION_RXN_SMARTS),
                )
            ),
            consumer=str(payload.get("consumer", "RxnResid")),
            energy_unit=str(payload.get("energy_unit", "database-native")),
            timing_unit=str(payload.get("timing_unit", "seconds")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "reaction_filter": self.reaction_filter,
            "cleaned_only": self.cleaned_only,
            "reaction_smarts": self.reaction_smarts,
            "destination": str(self.destination),
            "consumer": self.consumer,
            "energy_unit": self.energy_unit,
            "timing_unit": self.timing_unit,
            "api_contract": self.api_contract,
            "required_columns": [
                "mapped_reaction_smiles",
                "reaction_free_energy",
                "activation_free_energy",
                "reactant_compute_seconds",
                "transition_state_compute_seconds",
                "product_compute_seconds",
            ],
        }


# Preserve the former public name while making the new RxnResid contract
# explicit.  An alias also keeps positional construction unambiguous:
# ``(project_id, destination, ...)``.
ReactionSpaceExportRequest = RxnResidDatasetExportRequest


def record_from_mapping(payload: Mapping[str, Any]) -> ReactionRecord:
    raw_artifacts = payload.get("artifacts", ())
    artifacts = tuple(
        artifact if isinstance(artifact, Artifact) else Artifact(**artifact)
        for artifact in raw_artifacts
    )
    return ReactionRecord(
        reaction_id=str(payload.get("reaction_id", "")),
        reaction_smiles=str(payload.get("reaction_smiles", "")),
        reactants=tuple(str(value) for value in payload.get("reactants", ())),
        products=tuple(str(value) for value in payload.get("products", ())),
        conditions=dict(payload.get("conditions", {})),
        provenance=dict(payload.get("provenance", {})),
        artifacts=artifacts,
    )


@dataclass(frozen=True, slots=True)
class ReactionIngestRequest:
    record: ReactionRecord
    idempotency_key: str | None = None
    validate_only: bool = False
    source: str = "agent"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise InvalidRequestError("ingest source must not be empty")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ReactionIngestRequest:
        raw_record = payload.get("record", payload)
        if not isinstance(raw_record, Mapping):
            raise InvalidRequestError("record must be an object")
        return cls(
            record=record_from_mapping(raw_record),
            idempotency_key=payload.get("idempotency_key"),
            validate_only=bool(payload.get("validate_only", False)),
            source=str(payload.get("source", "agent")),
            metadata=dict(payload.get("metadata", {})),
        )
