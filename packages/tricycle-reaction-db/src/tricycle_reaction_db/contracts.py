from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

RXNRESID_EXPORT_API_CONTRACT = "reaction-db.mcp.mapped-reaction-thermodynamics.v1"
RITS_EXPORT_API_CONTRACT = "reaction-db.mcp.mapped-reaction-geometry.v1"


@dataclass(frozen=True, slots=True)
class ReactionDatabaseApiRequest:
    """Redacted proof of one REST request used to build an export artifact."""

    method: str
    route: str
    body: Mapping[str, object]
    response_content_type: str

    def __post_init__(self) -> None:
        method = str(self.method).strip().upper()
        route = str(self.route).strip()
        content_type = str(self.response_content_type).strip()
        if not method:
            raise ValueError("database API request method must not be empty")
        if not route.startswith("/"):
            raise ValueError("database API request route must be an absolute path")
        if not isinstance(self.body, Mapping):
            raise TypeError("database API request body must be a mapping")
        if not content_type:
            raise ValueError("database API response content type must not be empty")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "route", route)
        object.__setattr__(self, "body", dict(self.body))
        object.__setattr__(self, "response_content_type", content_type)

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "route": self.route,
            "body": dict(self.body),
            "response_content_type": self.response_content_type,
        }


@dataclass(frozen=True, slots=True)
class ReactionDatabaseMcpToolCall:
    """Redacted provenance for one database MCP tool operation.

    The arguments are intentionally a serializable description of the call,
    not a transport object.  Concrete MCP adapters may aggregate repeated
    paginated calls into one descriptor while retaining the exact project,
    predicate, and operation boundary used to produce an export.
    """

    tool_name: str
    arguments: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        tool_name = str(self.tool_name).strip()
        if not tool_name:
            raise ValueError("database MCP tool name must not be empty")
        if not isinstance(self.arguments, Mapping):
            raise TypeError("database MCP tool arguments must be a mapping")
        object.__setattr__(self, "tool_name", tool_name)
        object.__setattr__(self, "arguments", dict(self.arguments))

    def to_dict(self) -> dict[str, object]:
        return {"tool_name": self.tool_name, "arguments": dict(self.arguments)}
