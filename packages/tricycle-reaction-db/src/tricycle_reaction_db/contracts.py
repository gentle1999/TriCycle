from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

RXNRESID_EXPORT_API_CONTRACT = "reaction-db.mcp.mapped-reaction-thermodynamics.v1"
RITS_EXPORT_API_CONTRACT = "reaction-db.mcp.mapped-reaction-geometry.v1"


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
