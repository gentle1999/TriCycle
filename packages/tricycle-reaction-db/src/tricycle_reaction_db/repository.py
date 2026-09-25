from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from tricycle_core import ReactionRecord
from tricycle_core.errors import InvalidRequestError

from .contracts import (
    ReactionDatabaseApiRequest,
    ReactionDatabaseMcpToolCall,
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
    unproven local query-and-compose implementation from being reported as a
    successful export. The relevant REST or MCP request proof is recorded with
    the artifact.
    """

    destination: Path
    status: str = "succeeded"
    row_count: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    api_contract: str | None = None
    mcp_tool_calls: tuple[ReactionDatabaseMcpToolCall, ...] = ()
    api_request: ReactionDatabaseApiRequest | None = None

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
        mcp_tool_calls = tuple(self.mcp_tool_calls)
        if not all(isinstance(call, ReactionDatabaseMcpToolCall) for call in mcp_tool_calls):
            raise TypeError(
                "reaction-space export mcp_tool_calls must use the standard MCP call type"
            )
        object.__setattr__(self, "mcp_tool_calls", mcp_tool_calls)
        if self.api_request is not None and not isinstance(
            self.api_request, ReactionDatabaseApiRequest
        ):
            raise TypeError("reaction-space export api_request must use the standard API type")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, object]:
        return {
            "destination": str(self.destination),
            "status": self.status,
            "row_count": self.row_count,
            "metadata": dict(self.metadata),
            "api_contract": self.api_contract,
            "mcp_tool_calls": [call.to_dict() for call in self.mcp_tool_calls],
            "api_request": self.api_request.to_dict() if self.api_request is not None else None,
        }


def validate_export_receipt(
    request: RitsDatasetExportRequest | RxnResidDatasetExportRequest,
    receipt: ReactionSpaceExportReceipt,
) -> None:
    """Reject a successful export without proof for its standard database API."""

    if receipt.status.casefold().strip() not in {"succeeded", "success", "completed"}:
        return

    if receipt.api_contract != request.api_contract:
        raise InvalidRequestError(
            "successful reaction-space export must declare the request's standard database "
            f"API contract ({request.api_contract})"
        )
    if isinstance(request, RxnResidDatasetExportRequest):
        _validate_rxnresid_mcp_receipt(request, receipt)
        return
    if not receipt.mcp_tool_calls:
        raise InvalidRequestError(
            "successful RitS export must return database MCP request provenance"
        )
    _validate_mcp_export_receipt(request, receipt)


def _validate_rxnresid_mcp_receipt(
    request: RxnResidDatasetExportRequest,
    receipt: ReactionSpaceExportReceipt,
) -> None:
    """Validate project-scoped MCP provenance and the profile materialization policy."""

    if receipt.api_request is not None:
        raise InvalidRequestError("RxnResid MCP export must not claim REST request provenance")
    calls = receipt.mcp_tool_calls
    if len(calls) != 2 or any(call.tool_name != "compose_query" for call in calls):
        raise InvalidRequestError(
            "successful RxnResid export must include mapped-reaction and thermodynamics "
            "compose_query provenance"
        )
    by_operation = {call.arguments.get("operation"): call for call in calls}
    list_operation = "MappedReactionQueryService.list_mapped_reactions"
    thermo_operation = "ReactionEnergyQueryService.get_mapped_reaction_thermodynamics"
    if set(by_operation) != {list_operation, thermo_operation}:
        raise InvalidRequestError("RxnResid MCP export provenance has unexpected query methods")
    for call in calls:
        if call.arguments.get("project_id") != request.project_id:
            raise InvalidRequestError(
                "RxnResid MCP export must stay scoped to the requested project_id"
            )
        if call.arguments.get("reaction_smarts") != request.reaction_smarts:
            raise InvalidRequestError(
                "RxnResid MCP export provenance must preserve the configured reaction_smarts"
            )
        if not str(call.arguments.get("app_name", "")).strip():
            raise InvalidRequestError("RxnResid MCP export provenance must include app_name")

    list_call = by_operation[list_operation]
    thermo_call = by_operation[thermo_operation]
    if list_call.arguments.get("cleaned_only") is not True:
        raise InvalidRequestError("RxnResid MCP query provenance must record cleaned_only=true")
    page_count = list_call.arguments.get("page_count")
    mapped_count = list_call.arguments.get("mapped_reaction_count")
    batch_count = thermo_call.arguments.get("batch_count")
    queried_count = thermo_call.arguments.get("queried_reaction_count")
    max_aliases = thermo_call.arguments.get("max_aliases_per_query")
    for name, value in (
        ("page_count", page_count),
        ("mapped_reaction_count", mapped_count),
        ("batch_count", batch_count),
        ("queried_reaction_count", queried_count),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidRequestError(f"RxnResid MCP provenance has invalid {name}")
    if max_aliases != 24 or queried_count != mapped_count:
        raise InvalidRequestError(
            "RxnResid thermodynamics queries do not match the MCP batch contract"
        )
    if batch_count != (mapped_count + 23) // 24:
        raise InvalidRequestError("RxnResid thermodynamics batch count is inconsistent")

    metadata = receipt.metadata
    if metadata.get("transport") != "mcp":
        raise InvalidRequestError("RxnResid export metadata must record transport=mcp")
    if metadata.get("mcp_app_name") != list_call.arguments.get("app_name"):
        raise InvalidRequestError("RxnResid export metadata must record the MCP app name")
    if metadata.get("project_id") != request.project_id:
        raise InvalidRequestError("RxnResid export metadata must record the requested project_id")
    if metadata.get("reaction_smarts") != request.reaction_smarts:
        raise InvalidRequestError("RxnResid export metadata must preserve reaction_smarts")
    if metadata.get("cleaned_only") is not True:
        raise InvalidRequestError("RxnResid export metadata must record cleaned_only=true")
    if metadata.get("energy_unit") != "kcal/mol":
        raise InvalidRequestError("RxnResid export metadata must record energy_unit=kcal/mol")
    if metadata.get("timing_unit") != "seconds":
        raise InvalidRequestError("RxnResid export metadata must record timing_unit=seconds")
    if metadata.get("mapped_reaction_pages") != page_count:
        raise InvalidRequestError(
            "RxnResid export metadata has inconsistent mapped-reaction page count"
        )
    if metadata.get("mapped_reaction_count") != mapped_count:
        raise InvalidRequestError("RxnResid export metadata has inconsistent mapped-reaction count")
    if metadata.get("exported_rows") != receipt.row_count or receipt.row_count is None:
        raise InvalidRequestError("RxnResid export must record its exported row count")
    excluded = metadata.get("excluded_incomplete_thermodynamics_count")
    reasons = metadata.get("excluded_reasons")
    if isinstance(excluded, bool) or not isinstance(excluded, int) or excluded < 0:
        raise InvalidRequestError("RxnResid export must record its incomplete-thermodynamics count")
    if (
        not isinstance(reasons, Mapping)
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in reasons.values())
        or sum(reasons.values()) != excluded
    ):
        raise InvalidRequestError("RxnResid export must record exclusion counts by reason")


def _validate_mcp_export_receipt(
    request: RitsDatasetExportRequest,
    receipt: ReactionSpaceExportReceipt,
) -> None:
    """Validate database reaction filtering plus the paged geometry MCP tool."""

    filter_calls = [call for call in receipt.mcp_tool_calls if call.tool_name == "compose_query"]
    geometry_calls = [
        call
        for call in receipt.mcp_tool_calls
        if call.tool_name == "export_mapped_reaction_transition_state_geometries"
    ]
    if len(filter_calls) != 1 or len(geometry_calls) != 1 or len(receipt.mcp_tool_calls) != 2:
        raise InvalidRequestError(
            "RitS MCP export provenance must include database reaction filtering and the "
            "mapped-reaction geometry MCP export"
        )

    for call in receipt.mcp_tool_calls:
        if call.arguments.get("project_id") != request.project_id:
            raise InvalidRequestError(
                "database MCP export provenance must stay scoped to the requested project_id"
            )

    filter_call = filter_calls[0]
    if not str(filter_call.arguments.get("app_name", "")).strip():
        raise InvalidRequestError("database MCP filter provenance must include app_name")
    if (
        filter_call.arguments.get("operation")
        != "MappedReactionQueryService.list_mapped_reactions"
    ):
        raise InvalidRequestError("RitS MCP export must use the database reaction_smarts filter")
    if filter_call.arguments.get("reaction_smarts") != request.reaction_smarts:
        raise InvalidRequestError(
            "database MCP export provenance must preserve the configured reaction_smarts"
        )

    geometry_call = geometry_calls[0]
    page_size = geometry_call.arguments.get("limit")
    page_count = geometry_call.arguments.get("page_count")
    record_count = geometry_call.arguments.get("record_count")
    matched_record_count = geometry_call.arguments.get("matched_record_count")
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or page_size < 1
        or isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or page_count < 0
        or isinstance(record_count, bool)
        or not isinstance(record_count, int)
        or record_count < 0
        or isinstance(matched_record_count, bool)
        or not isinstance(matched_record_count, int)
        or matched_record_count < 0
    ):
        raise InvalidRequestError("RitS MCP geometry export provenance has invalid page counts")
    if matched_record_count != receipt.row_count:
        raise InvalidRequestError("RitS MCP geometry export provenance has inconsistent row counts")


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
    RxnResid and RitS query the database through the injected MCP adapter.
    A successful export receipt without matching request provenance is rejected by
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
