from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# These are Core API routes, not MCP tool names.  Keep them here so an adapter
# cannot quietly replace the database export with a local query-and-compose
# implementation.
RXNRESID_STANDARD_CSV_EXPORT_ROUTE = "/api/mapped-reactions/thermodynamics/export.csv"
RXNRESID_STANDARD_CSV_EXPORT_METHOD = "POST"
RXNRESID_EXPORT_API_CONTRACT = "core-api.mapped-reaction-thermodynamics-csv.v1"

RITS_STANDARD_MAPPED_REACTIONS_ROUTE = "/api/mapped-reactions"
RITS_STANDARD_MAPPED_REACTION_DETAIL_ROUTE = "/api/mapped-reactions/{mapped_reaction_id}"
RITS_STANDARD_GEOMETRY_XYZ_ROUTE = "/api/depictions/geometry/{geometry_id}.xyz"
RITS_EXPORT_API_CONTRACT = "core-api.mapped-reaction-geometry.v1"

REACTION_SMARTS_FILTER_FIELD = "reaction_smarts"


def build_filter_expression(reaction_smarts: str) -> str:
    """Build the Core API's JSON-string filter expression.

    ``filter_expression`` is deliberately serialized here because the
    thermodynamics CSV endpoint accepts a JSON string in its POST body.  An
    adapter must not substitute ``reaction_class`` or a locally interpreted
    label for this expression.
    """

    return json.dumps(
        {"field": REACTION_SMARTS_FILTER_FIELD, "value": reaction_smarts},
        ensure_ascii=False,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class StandardReactionDatabaseApiRequest:
    """A redacted, serializable record of one required database API call.

    The request is a contract/provenance object, not an HTTP client.  The
    concrete adapter performs the call and returns these exact request shapes
    in ``ReactionSpaceExportReceipt`` so the plugin can reject an incorrect
    endpoint before declaring an export successful.
    """

    method: str
    route: str
    query: Mapping[str, object] = field(default_factory=dict)
    body: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        method = str(self.method).strip().upper()
        route = str(self.route).strip()
        if not method:
            raise ValueError("database API request method must not be empty")
        if not route.startswith("/"):
            raise ValueError("database API request route must be an absolute API path")
        if not isinstance(self.query, Mapping):
            raise TypeError("database API request query must be a mapping")
        if not isinstance(self.body, Mapping):
            raise TypeError("database API request body must be a mapping")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "route", route)
        object.__setattr__(self, "query", dict(self.query))
        object.__setattr__(self, "body", dict(self.body))

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "route": self.route,
            "query": dict(self.query),
            "body": dict(self.body),
        }


def build_rxnresid_csv_export_request(
    project_id: str,
    reaction_smarts: str,
) -> StandardReactionDatabaseApiRequest:
    """Return the only supported RxnResid CSV export request shape."""

    return StandardReactionDatabaseApiRequest(
        method=RXNRESID_STANDARD_CSV_EXPORT_METHOD,
        route=RXNRESID_STANDARD_CSV_EXPORT_ROUTE,
        body={
            "project_id": project_id,
            "filter_expression": build_filter_expression(reaction_smarts),
            "has_activation_gibbs_free_energy": True,
            "has_reaction_gibbs_free_energy": True,
        },
    )


def build_rits_mapped_reaction_requests(
    project_id: str,
    reaction_smarts: str,
) -> tuple[StandardReactionDatabaseApiRequest, ...]:
    """Return the standard resource calls needed for the RitS CSV.

    The current database exposes mapped reactions and XYZ geometry as resource
    APIs, but no combined mapped-reaction-plus-TS CSV route.  The adapter may
    materialize the local CSV only after reading these standard resources.
    """

    return (
        StandardReactionDatabaseApiRequest(
            method="GET",
            route=RITS_STANDARD_MAPPED_REACTIONS_ROUTE,
            query={"project_id": project_id, REACTION_SMARTS_FILTER_FIELD: reaction_smarts},
        ),
        StandardReactionDatabaseApiRequest(
            method="GET",
            route=RITS_STANDARD_MAPPED_REACTION_DETAIL_ROUTE,
            query={"project_id": project_id},
        ),
        StandardReactionDatabaseApiRequest(
            method="GET",
            route=RITS_STANDARD_GEOMETRY_XYZ_ROUTE,
            query={"project_id": project_id},
        ),
    )

