from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from tricycle_core import ReactionRecord
from tricycle_core.errors import InvalidRequestError

from .contracts import (
    StandardReactionDatabaseApiRequest,
    build_rits_mapped_reaction_requests,
    build_rxnresid_csv_export_request,
)
from .models import (
    CYCLOADDITION_RXN_SMARTS,
    CalculationLogBatchManifest,
    ReactionBatchImportRequest,
    ReactionFilterRequest,
    RitsDatasetExportRequest,
    RxnResidDatasetExportRequest,
)


@dataclass(frozen=True, slots=True)
class ReactionDatabaseImportReceipt:
    """Adapter result for one project-scoped calculation-log import."""

    status: str = "succeeded"
    project_id: str | None = None
    accepted_log_count: int = 0
    failed_log_count: int = 0
    cleaned_reaction_count: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.status.strip():
            raise ValueError("database import status must not be empty")
        if self.status.casefold().strip() in {"succeeded", "success", "completed"} and not (
            self.project_id and self.project_id.strip()
        ):
            raise ValueError("a successful project import must return the internal project_id")
        if self.accepted_log_count < 0 or self.failed_log_count < 0:
            raise ValueError("database import counts must not be negative")
        if self.cleaned_reaction_count is not None and self.cleaned_reaction_count < 0:
            raise ValueError("cleaned reaction count must not be negative")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "project_id": self.project_id,
            "accepted_log_count": self.accepted_log_count,
            "failed_log_count": self.failed_log_count,
            "cleaned_reaction_count": self.cleaned_reaction_count,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ReactionSpaceExportReceipt:
    """Adapter result for a database-authoritative dataset export.

    A successful receipt must identify the standard database API contract and
    the canonical request shapes used by the adapter.  This prevents a
    local/MCP query-and-compose implementation from being reported as a
    successful standard export.
    """

    destination: Path
    status: str = "succeeded"
    row_count: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    api_contract: str | None = None
    api_requests: tuple[StandardReactionDatabaseApiRequest, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.destination, Path):
            object.__setattr__(self, "destination", Path(str(self.destination)))
        if not self.status.strip():
            raise ValueError("reaction-space export status must not be empty")
        if self.row_count is not None and self.row_count < 0:
            raise ValueError("reaction-space row count must not be negative")
        if self.api_contract is not None:
            contract = str(self.api_contract).strip()
            object.__setattr__(self, "api_contract", contract or None)
        requests = tuple(self.api_requests)
        if not all(isinstance(request, StandardReactionDatabaseApiRequest) for request in requests):
            raise TypeError("reaction-space export api_requests must use the standard API request type")
        object.__setattr__(self, "api_requests", requests)
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, object]:
        return {
            "destination": str(self.destination),
            "status": self.status,
            "row_count": self.row_count,
            "metadata": dict(self.metadata),
            "api_contract": self.api_contract,
            "api_requests": [request.to_dict() for request in self.api_requests],
        }


def validate_export_receipt(
    request: RitsDatasetExportRequest | RxnResidDatasetExportRequest,
    receipt: ReactionSpaceExportReceipt,
) -> None:
    """Reject a successful export that did not use the standard DB API.

    The adapter remains transport-injected, but the successful result is no
    longer transport-agnostic: it must prove the route and request shape that
    produced the local artifact.  This is intentionally checked at the plugin
    boundary so every adapter implementation gets the same guard.
    """

    if receipt.status.casefold().strip() not in {"succeeded", "success", "completed"}:
        return

    if receipt.api_contract != request.api_contract:
        raise InvalidRequestError(
            "successful reaction-space export must declare the request's standard database "
            f"API contract ({request.api_contract})"
        )
    if not receipt.api_requests:
        raise InvalidRequestError(
            "successful reaction-space export must return standard database API request provenance"
        )

    for api_request in receipt.api_requests:
        keys = set(api_request.query) | set(api_request.body)
        if "reaction_class" in keys:
            raise InvalidRequestError(
                "reaction-space export provenance contains reaction_class; use reaction_smarts"
            )

    if isinstance(request, RxnResidDatasetExportRequest):
        expected = build_rxnresid_csv_export_request(
            request.project_id,
            request.reaction_smarts,
        )
        if expected not in receipt.api_requests:
            raise InvalidRequestError(
                "RxnResid export must use POST "
                "`/api/mapped-reactions/thermodynamics/export.csv` with the project-scoped "
                "reaction_smarts filter"
            )
        return

    expected_requests = build_rits_mapped_reaction_requests(
        request.project_id,
        request.reaction_smarts,
    )
    if not all(expected in receipt.api_requests for expected in expected_requests):
        raise InvalidRequestError(
            "RitS export must use the standard mapped-reaction and XYZ geometry API routes; "
            "the database has no combined CSV route"
        )


@dataclass(frozen=True, slots=True)
class ReactionFilterReceipt:
    """Adapter result for the database-authoritative rxn-SMARTS selection."""

    project_id: str
    reaction_filter: str = "cycloaddition"
    selected_count: int | None = None
    reaction_smarts: str = CYCLOADDITION_RXN_SMARTS
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.project_id.strip():
            raise ValueError("reaction filter project_id must not be empty")
        if self.selected_count is not None and self.selected_count < 0:
            raise ValueError("selected reaction count must not be negative")
        if self.reaction_smarts != CYCLOADDITION_RXN_SMARTS:
            raise ValueError("reaction filter receipt must preserve the configured reaction_smarts")
        object.__setattr__(self, "reaction_smarts", CYCLOADDITION_RXN_SMARTS)
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "reaction_filter": self.reaction_filter,
            "selected_count": self.selected_count,
            "reaction_smarts": self.reaction_smarts,
            "metadata": dict(self.metadata),
        }


class ReactionDatabaseAdapter(Protocol):
    """Injected boundary for the remote/MCP reaction database.

    The plugin supplies only the validated, resolved log manifests.  The
    adapter owns project creation, database ingestion, and the authoritative
    cleaned-reaction export protocol; no remote schema is duplicated here.
    For reaction selection and exports, the adapter must pass
    ``request.reaction_smarts`` to the database's rxn-SMARTS predicate.  The
    ``reaction_filter`` value is only a local operation label and must not be
    translated into a ``reaction_class`` query.

    Export implementations must use the request builders in ``contracts.py``.
    RxnResid uses the database's standard thermodynamics CSV POST route;
    RitS uses the standard mapped-reaction/detail/geometry resource routes
    because the database currently has no combined RitS CSV route.  A
    successful export receipt without matching API provenance is rejected by
    ``validate_export_receipt``.
    """

    def ingest_calculation_logs(
        self,
        request: ReactionBatchImportRequest,
        manifest: CalculationLogBatchManifest,
    ) -> ReactionDatabaseImportReceipt: ...

    def filter_cleaned_reactions(
        self,
        request: ReactionFilterRequest,
    ) -> ReactionFilterReceipt: ...

    def export_rits_dataset(
        self,
        request: RitsDatasetExportRequest,
    ) -> ReactionSpaceExportReceipt: ...

    def export_rxnresid_dataset(
        self,
        request: RxnResidDatasetExportRequest,
    ) -> ReactionSpaceExportReceipt: ...


class InMemoryReactionRepository:
    """Small fake repository for contract tests; replace with a DB/API adapter later."""

    def __init__(self) -> None:
        self.records: dict[str, ReactionRecord] = {}

    def upsert(self, record: ReactionRecord) -> str:
        self.records[record.reaction_id] = record
        return record.reaction_id

    def get(self, reaction_id: str) -> ReactionRecord | None:
        return self.records.get(reaction_id)
