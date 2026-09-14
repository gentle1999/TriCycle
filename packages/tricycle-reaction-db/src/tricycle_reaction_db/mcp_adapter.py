"""MCP adapter for the project-scoped reaction database.

The database exposes a Streamable HTTP MCP endpoint.  This module contains a
small synchronous MCP client so the domain package can keep its existing
``ReactionDatabaseAdapter`` boundary without depending on a vendor SDK.  Every
calculation log is uploaded through the database's file-upload MCP tool; the
adapter never relies on a shared filesystem between the agent and database.
"""

from __future__ import annotations

import base64
import csv
import json
import os
import ssl
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from tricycle_core import IntegrationNotConfiguredError, InvalidRequestError, load_dotenv
from tricycle_core.errors import TriCycleError

from .contracts import (
    RITS_EXPORT_API_CONTRACT,
    RXNRESID_EXPORT_API_CONTRACT,
    ReactionDatabaseMcpToolCall,
)
from .models import (
    CalculationLogBatchManifest,
    ReactionBatchImportRequest,
    ReactionFilterRequest,
    RitsDatasetExportRequest,
    RxnResidDatasetExportRequest,
)
from .repository import (
    ReactionDatabaseAdapter,
    ReactionDatabaseImportReceipt,
    ReactionFilterReceipt,
    ReactionSpaceExportReceipt,
)

_MCP_PROTOCOL_VERSION = "2025-03-26"
_DEFAULT_APP_NAME = "example-chemistry-database"
_MCP_RATE_LIMIT_RETRIES = 8
_MCP_MAX_RETRY_AFTER_SECONDS = 60.0
_QUEUE_ACCEPTED_STATUSES = frozenset(
    {
        "queued",
        "pending",
        "uploading",
        "staged",
        "processing",
        "succeeded",
        "success",
        "completed",
    }
)
_QUEUE_FAILED_STATUSES = frozenset({"failed", "error", "cancelled", "canceled"})
_MCP_ENV_KEYS = frozenset(
    {
        "TRICYCLE_REACTION_DB_MCP_URL",
        "TRICYCLE_REACTION_DB_URL",
        "TRICYCLE_REACTION_DB_MCP_TOKEN",
        "TRICYCLE_REACTION_DB_TOKEN",
        "TRICYCLE_REACTION_DB_ORGANIZATION_ID",
        "TRICYCLE_REACTION_DB_MCP_APP_NAME",
        "TRICYCLE_REACTION_DB_MCP_VERIFY_TLS",
        "TRICYCLE_REACTION_DB_VERIFY_TLS",
        "TRICYCLE_REACTION_DB_MCP_CA_BUNDLE",
        "TRICYCLE_REACTION_DB_CA_BUNDLE",
        "TRICYCLE_REACTION_DB_MCP_TIMEOUT_SECONDS",
        "TRICYCLE_REACTION_DB_TIMEOUT_SECONDS",
        "TRICYCLE_REACTION_DB_MCP_PAGE_SIZE",
    }
)


class ReactionDatabaseMcpError(TriCycleError):
    """A transport, JSON-RPC, or database-MCP contract error."""

    def __init__(
        self,
        message: str,
        *,
        code: str | int | None = None,
        status_code: int | None = None,
        method: str | None = None,
        tool_name: str | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.method = method
        self.tool_name = tool_name
        super().__init__(message)


def _env_bool(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise InvalidRequestError(f"{name} must be a boolean")


def _env_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise InvalidRequestError(f"{name} must be a number") from exc


def _env_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise InvalidRequestError(f"{name} must be an integer") from exc


@dataclass(frozen=True, slots=True)
class ReactionDatabaseMcpConfig:
    """Runtime settings for :class:`McpReactionDatabaseAdapter`.

    Calculation-log bytes are transferred through MCP.  No path in the
    database service's filesystem is part of this configuration.
    """

    endpoint_url: str
    token: str
    organization_id: str | None = None
    app_name: str = _DEFAULT_APP_NAME
    verify_tls: bool = True
    ca_bundle: Path | None = None
    timeout_seconds: float = 60.0
    page_size: int = 200

    def __post_init__(self) -> None:
        endpoint = str(self.endpoint_url).strip().rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise InvalidRequestError("TRICYCLE_REACTION_DB_MCP_URL must be an http(s) URL")
        if parsed.query or parsed.fragment:
            raise InvalidRequestError(
                "TRICYCLE_REACTION_DB_MCP_URL must not contain a query or fragment"
            )
        if not parsed.path.rstrip("/").endswith("/mcp"):
            endpoint = f"{endpoint}/mcp"
        endpoint = f"{endpoint}/"
        token = str(self.token).strip()
        if not token:
            raise IntegrationNotConfiguredError(
                "TRICYCLE_REACTION_DB_MCP_TOKEN is required for live database operations"
            )
        app_name = str(self.app_name).strip()
        if not app_name:
            raise InvalidRequestError("TRICYCLE_REACTION_DB_MCP_APP_NAME must not be empty")
        organization_id = self.organization_id
        if organization_id is not None:
            organization_id = str(organization_id).strip() or None
        if self.timeout_seconds <= 0:
            raise InvalidRequestError("timeout_seconds must be positive")
        if not 1 <= self.page_size <= 200:
            raise InvalidRequestError("page_size must be between 1 and 200")
        ca_bundle = self.ca_bundle
        if ca_bundle is not None and not isinstance(ca_bundle, Path):
            ca_bundle = Path(str(ca_bundle)).expanduser()
        if self.verify_tls and ca_bundle is not None and not ca_bundle.is_file():
            raise InvalidRequestError(f"database CA bundle does not exist: {ca_bundle}")
        object.__setattr__(self, "endpoint_url", endpoint)
        object.__setattr__(self, "token", token)
        object.__setattr__(self, "organization_id", organization_id)
        object.__setattr__(self, "app_name", app_name)
        object.__setattr__(self, "ca_bundle", ca_bundle)

    @classmethod
    def from_env(
        cls,
        env_file: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> ReactionDatabaseMcpConfig:
        """Build live MCP settings only from the process environment and ``.env``."""

        values = dict(os.environ if environ is None else environ)
        if environ is None or env_file is not None:
            load_dotenv(env_file, environ=values, keys=_MCP_ENV_KEYS)
        raw_endpoint = values.get("TRICYCLE_REACTION_DB_MCP_URL", "").strip()
        if not raw_endpoint:
            raw_endpoint = values.get("TRICYCLE_REACTION_DB_URL", "").strip()
        token = values.get("TRICYCLE_REACTION_DB_MCP_TOKEN", "").strip()
        if not token:
            token = values.get("TRICYCLE_REACTION_DB_TOKEN", "").strip()
        if not raw_endpoint:
            raise IntegrationNotConfiguredError(
                "TRICYCLE_REACTION_DB_MCP_URL or TRICYCLE_REACTION_DB_URL is required "
                "for live database operations"
            )
        if not token:
            raise IntegrationNotConfiguredError(
                "TRICYCLE_REACTION_DB_MCP_TOKEN or TRICYCLE_REACTION_DB_TOKEN is required "
                "for live database operations"
            )
        ca_bundle_value = values.get("TRICYCLE_REACTION_DB_MCP_CA_BUNDLE", "").strip()
        if not ca_bundle_value:
            ca_bundle_value = values.get("TRICYCLE_REACTION_DB_CA_BUNDLE", "").strip()
        return cls(
            endpoint_url=raw_endpoint,
            token=token,
            organization_id=values.get("TRICYCLE_REACTION_DB_ORGANIZATION_ID"),
            app_name=values.get("TRICYCLE_REACTION_DB_MCP_APP_NAME", _DEFAULT_APP_NAME),
            verify_tls=_env_bool(
                values,
                "TRICYCLE_REACTION_DB_MCP_VERIFY_TLS",
                _env_bool(values, "TRICYCLE_REACTION_DB_VERIFY_TLS", True),
            ),
            ca_bundle=Path(ca_bundle_value).expanduser() if ca_bundle_value else None,
            timeout_seconds=_env_float(
                values,
                "TRICYCLE_REACTION_DB_MCP_TIMEOUT_SECONDS",
                _env_float(values, "TRICYCLE_REACTION_DB_TIMEOUT_SECONDS", 60.0),
            ),
            page_size=_env_int(values, "TRICYCLE_REACTION_DB_MCP_PAGE_SIZE", 200),
        )

    def ssl_context(self) -> ssl.SSLContext:
        if not self.verify_tls:
            return ssl._create_unverified_context()
        if self.ca_bundle is not None:
            return ssl.create_default_context(cafile=str(self.ca_bundle))
        return ssl.create_default_context()


class _McpTransport:
    """Minimal Streamable HTTP MCP transport for synchronous adapters."""

    def __init__(self, config: ReactionDatabaseMcpConfig) -> None:
        self.config = config
        self._request_id = 0
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._initialized = False
        self._tool_names: frozenset[str] | None = None

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> Mapping[str, Any]:
        self.ensure_tool(name)
        response = self._rpc(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ReactionDatabaseMcpError(
                f"MCP tools/call returned no result for {name}",
                method="tools/call",
                tool_name=name,
            )
        return self._tool_result(result, name)

    def ensure_tool(self, name: str) -> None:
        """Initialize the session and fail before mutation if a tool is absent."""

        self._ensure_initialized()
        if self._tool_names is not None and name not in self._tool_names:
            raise ReactionDatabaseMcpError(
                f"database MCP server does not expose required tool {name}",
                method="tools/list",
                tool_name=name,
            )

    def compose_query(self, app_name: str, query: str) -> Mapping[str, Any]:
        payload = self.call_tool("compose_query", {"app_name": app_name, "query": query})
        errors = payload.get("errors")
        if errors:
            raise ReactionDatabaseMcpError(
                f"database compose_query returned errors: {self._error_text(errors)}",
                tool_name="compose_query",
            )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ReactionDatabaseMcpError(
                "database compose_query returned no data",
                tool_name="compose_query",
            )
        return data

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        response = self._rpc(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "tricycle-reaction-db", "version": "0.1.0"},
            },
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ReactionDatabaseMcpError("MCP initialize returned no result", method="initialize")
        protocol_version = str(result.get("protocolVersion", "")).strip()
        if not protocol_version:
            raise ReactionDatabaseMcpError("MCP initialize returned no protocolVersion")
        self._protocol_version = protocol_version
        self._initialized = True
        self._rpc("notifications/initialized", {}, notification=True)
        tools_response = self._rpc("tools/list", {})
        tools_result = tools_response.get("result")
        if not isinstance(tools_result, Mapping):
            raise ReactionDatabaseMcpError("MCP tools/list returned no result", method="tools/list")
        raw_tools = tools_result.get("tools")
        if not isinstance(raw_tools, list):
            raise ReactionDatabaseMcpError("MCP tools/list returned no tools", method="tools/list")
        names = {
            str(tool.get("name"))
            for tool in raw_tools
            if isinstance(tool, Mapping) and tool.get("name")
        }
        self._tool_names = frozenset(names)

    def _rpc(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        notification: bool = False,
    ) -> Mapping[str, Any]:
        request_id: int | None = None
        body: dict[str, object] = {"jsonrpc": "2.0", "method": method, "params": dict(params)}
        if not notification:
            self._request_id += 1
            request_id = self._request_id
            body["id"] = request_id
        raw, headers = self._post(body)
        if notification:
            return {}
        messages = self._decode_messages(raw, headers.get("content-type", ""))
        for message in messages:
            if message.get("id") != request_id:
                continue
            error = message.get("error")
            if isinstance(error, Mapping):
                raise ReactionDatabaseMcpError(
                    f"MCP {method} failed: {self._error_text(error)}",
                    code=error.get("code"),
                    method=method,
                )
            return message
        raise ReactionDatabaseMcpError(f"MCP {method} returned no matching response", method=method)

    def _post(self, body: Mapping[str, object]) -> tuple[bytes, Mapping[str, str]]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.token}",
            "User-Agent": "TriCycle-reaction-db-mcp/0.1",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        for attempt in range(_MCP_RATE_LIMIT_RETRIES + 1):
            request = Request(self.config.endpoint_url, data=data, headers=headers, method="POST")
            try:
                with urlopen(
                    request,
                    timeout=self.config.timeout_seconds,
                    context=self.config.ssl_context()
                    if urlsplit(self.config.endpoint_url).scheme == "https"
                    else None,
                ) as response:
                    raw = response.read()
                    response_headers = {
                        str(key).casefold(): str(value) for key, value in response.headers.items()
                    }
                    session_id = response_headers.get("mcp-session-id")
                    if session_id:
                        self._session_id = session_id
                    return raw, response_headers
            except HTTPError as exc:
                detail = exc.read(2048).decode("utf-8", errors="replace").strip()
                if exc.code == 429 and attempt < _MCP_RATE_LIMIT_RETRIES:
                    time.sleep(self._retry_after_seconds(exc.headers, detail))
                    continue
                suffix = f": {detail[:500]}" if detail else ""
                raise ReactionDatabaseMcpError(
                    f"database MCP HTTP {exc.code}{suffix}",
                    status_code=exc.code,
                ) from exc
            except (URLError, TimeoutError, OSError) as exc:
                raise ReactionDatabaseMcpError(f"database MCP request failed: {exc}") from exc

        raise AssertionError("MCP rate-limit retry loop exited unexpectedly")

    @staticmethod
    def _retry_after_seconds(headers: Any, detail: str) -> float:
        """Read the server backoff without trusting an unbounded delay."""

        candidates: list[object] = []
        if headers is not None:
            candidates.append(headers.get("Retry-After"))
        try:
            payload = json.loads(detail)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, Mapping):
            nested = payload.get("detail")
            if isinstance(nested, Mapping):
                candidates.append(nested.get("retry_after_seconds"))
            candidates.append(payload.get("retry_after_seconds"))
        for candidate in candidates:
            try:
                value = float(str(candidate).strip())
            except (TypeError, ValueError):
                continue
            if value >= 0:
                return min(value, _MCP_MAX_RETRY_AFTER_SECONDS)
        return 1.0

    @staticmethod
    def _decode_messages(raw: bytes, content_type: str) -> list[Mapping[str, Any]]:
        if not raw:
            return []
        if content_type.casefold().startswith("text/event-stream"):
            messages: list[Mapping[str, Any]] = []
            for line in raw.decode("utf-8", errors="replace").splitlines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].lstrip()
                if not data or data == "[DONE]":
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise ReactionDatabaseMcpError("database MCP returned invalid SSE JSON") from exc
                if isinstance(payload, Mapping):
                    messages.append(payload)
            return messages
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReactionDatabaseMcpError("database MCP returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ReactionDatabaseMcpError("database MCP response must be a JSON object")
        return [payload]

    def _tool_result(self, result: Mapping[str, Any], name: str) -> Mapping[str, Any]:
        if result.get("isError") is True:
            raise ReactionDatabaseMcpError(
                f"database MCP tool {name} failed: {self._content_text(result.get('content'))}",
                tool_name=name,
            )
        payload = result.get("structuredContent")
        if not isinstance(payload, Mapping):
            payload = self._parse_content_payload(result.get("content"), name)
        if payload.get("success") is False:
            error = payload.get("error")
            raise ReactionDatabaseMcpError(
                f"database MCP tool {name} failed: {self._error_text(error or payload)}",
                code=error.get("code") if isinstance(error, Mapping) else None,
                tool_name=name,
            )
        if payload.get("success") is True and isinstance(payload.get("data"), Mapping):
            return payload["data"]
        return payload

    @classmethod
    def _parse_content_payload(cls, content: object, name: str) -> Mapping[str, Any]:
        if not isinstance(content, list):
            raise ReactionDatabaseMcpError(
                f"database MCP tool {name} returned no structured content",
                tool_name=name,
            )
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "text":
                continue
            text = str(block.get("text", ""))
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, Mapping):
                return payload
        raise ReactionDatabaseMcpError(
            f"database MCP tool {name} returned no JSON content",
            tool_name=name,
        )

    @staticmethod
    def _content_text(content: object) -> str:
        if not isinstance(content, list):
            return str(content or "")[:500]
        return " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text"
        )[:500]

    @staticmethod
    def _error_text(error: object) -> str:
        if isinstance(error, Mapping):
            return str(error.get("message") or error.get("detail") or error)[:1000]
        if isinstance(error, list):
            return "; ".join(_McpTransport._error_text(item) for item in error)[:1000]
        return str(error)[:1000]


@dataclass(frozen=True, slots=True)
class _UploadFile:
    """Content-addressed local file metadata prepared before remote mutation."""

    path: Path
    relative_path: str
    size_bytes: int
    file_sha256: str


class McpReactionDatabaseAdapter(ReactionDatabaseAdapter):
    """Concrete adapter using the database's Streamable HTTP MCP endpoint."""

    def __init__(self, config: ReactionDatabaseMcpConfig) -> None:
        self.config = config
        self._transport = _McpTransport(config)

    @classmethod
    def from_env(
        cls,
        env_file: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> McpReactionDatabaseAdapter:
        return cls(ReactionDatabaseMcpConfig.from_env(env_file, environ=environ))

    def ingest_calculation_logs(
        self,
        request: ReactionBatchImportRequest,
        manifest: CalculationLogBatchManifest,
    ) -> ReactionDatabaseImportReceipt:
        organization_id = request.project.organization_id or self.config.organization_id
        if not organization_id:
            raise IntegrationNotConfiguredError(
                "reaction database imports require project.organization_id or "
                "TRICYCLE_REACTION_DB_ORGANIZATION_ID"
            )
        upload_files, archive_sha256 = self._build_upload_files(manifest)
        self._transport.ensure_tool("create_project")
        self._transport.ensure_tool("upload_calculation_log")
        idempotency_key = request.resolved_idempotency_key(manifest)
        project = self._transport.call_tool(
            "create_project",
            {
                "organization_id": organization_id,
                "slug": request.project.slug,
                "name": request.project.name,
                "data_source": {
                    "adapter": "tricycle-reaction-db.mcp",
                    "idempotency_key": idempotency_key,
                    "source": request.source,
                    "batch_id": manifest.batch_id,
                    "manifest_digest": manifest.manifest_digest,
                    "archive_sha256": archive_sha256,
                    "batch_metadata": dict(manifest.metadata),
                    "request_metadata": dict(request.metadata),
                    "file_transfer": "mcp.upload_calculation_log.base64",
                    "file_count": len(upload_files),
                    "total_bytes": manifest.total_bytes,
                },
                "calculation_protocol": {
                    "source": request.source,
                    "batch_id": manifest.batch_id,
                    "provenance": dict(request.provenance),
                    "metadata": dict(request.metadata),
                },
            },
        )
        project_id = self._required_identifier(project, "project", ("project_id", "id"))

        accepted_count = 0
        failed_count = 0
        uploaded_count = 0
        queue_item_status_counts: dict[str, int] = {}
        queue_batch_status_counts: dict[str, int] = {}
        queue_submissions: list[dict[str, str]] = []
        errors: list[str] = []
        for upload_file in upload_files:
            try:
                payload = self._read_file_payload(upload_file)
                result = self._transport.call_tool(
                    "upload_calculation_log",
                    {
                        "project_id": project_id,
                        "filename": upload_file.path.name,
                        "content_base64": base64.b64encode(payload).decode("ascii"),
                        "media_type": "text/plain",
                        "relative_path": upload_file.relative_path,
                        "expected_sha256": upload_file.file_sha256,
                        "expected_size_bytes": upload_file.size_bytes,
                    },
                )
            except (InvalidRequestError, OSError) as exc:
                failed_count += 1
                errors.append(f"{upload_file.relative_path}: {exc}")
                continue
            except ReactionDatabaseMcpError as exc:
                if not self._is_file_upload_error(exc):
                    raise
                failed_count += 1
                errors.append(f"{upload_file.relative_path}: {exc}")
                continue

            uploaded_count += 1
            try:
                if not isinstance(result, Mapping):
                    raise ReactionDatabaseMcpError(
                        "upload_calculation_log returned no durable queue batch/item",
                        tool_name="upload_calculation_log",
                    )
                batch = result.get("batch")
                item = result.get("item")
                if not isinstance(batch, Mapping) or not isinstance(item, Mapping):
                    raise ReactionDatabaseMcpError(
                        "upload_calculation_log returned no durable queue batch/item",
                        tool_name="upload_calculation_log",
                    )
                batch_id = self._required_identifier(batch, "upload batch", ("id", "batch_id"))
                item_id = self._required_identifier(
                    item,
                    "upload batch item",
                    ("id", "item_id", "client_file_id"),
                )
            except ReactionDatabaseMcpError as exc:
                failed_count += 1
                errors.append(f"{upload_file.relative_path}: {exc}")
                continue

            item_status = self._status_value(item) or "unknown"
            batch_status = self._status_value(batch) or "unknown"
            queue_item_status_counts[item_status] = queue_item_status_counts.get(item_status, 0) + 1
            queue_batch_status_counts[batch_status] = (
                queue_batch_status_counts.get(batch_status, 0) + 1
            )
            queue_submissions.append(
                {
                    "relative_path": upload_file.relative_path,
                    "batch_id": batch_id,
                    "item_id": item_id,
                    "item_status": item_status,
                    "batch_status": batch_status,
                }
            )
            if item_status in _QUEUE_ACCEPTED_STATUSES:
                accepted_count += 1
            else:
                failed_count += 1
                status_kind = "failed" if item_status in _QUEUE_FAILED_STATUSES else "unrecognized"
                errors.append(
                    f"{upload_file.relative_path}: database queue item status={item_status}"
                    f" ({status_kind})"
                )

        status = "submitted" if failed_count == 0 else "failed"
        metadata: dict[str, object] = {
            "transport": "mcp",
            "mcp_endpoint": self.config.endpoint_url,
            "mcp_app_name": self.config.app_name,
            "mcp_tools": ["create_project", "upload_calculation_log"],
            "file_transfer": "base64-per-file-queue",
            "queue_mode": "durable-upload-batch",
            "manifest_digest": manifest.manifest_digest,
            "archive_sha256": archive_sha256,
            "total_count": len(manifest.files),
            "uploaded_count": uploaded_count,
            "accepted_count": accepted_count,
            "failed_count": failed_count,
            "queue_batch_count": len(queue_submissions),
            "queue_item_status_counts": queue_item_status_counts,
            "queue_batch_status_counts": queue_batch_status_counts,
            "queue_submissions": queue_submissions,
            "parse_completion": "not_waited",
        }
        if errors:
            metadata["errors"] = errors[:20]
            metadata["error_count"] = len(errors)
        return ReactionDatabaseImportReceipt(
            status=status,
            project_id=project_id,
            accepted_log_count=accepted_count,
            failed_log_count=failed_count,
            cleaned_reaction_count=None,
            metadata=metadata,
        )

    def filter_cleaned_reactions(self, request: ReactionFilterRequest) -> ReactionFilterReceipt:
        query = self._mapped_reaction_list_query(
            request.project_id,
            request.reaction_smarts,
            limit=1,
            offset=0,
        )
        data = self._transport.compose_query(self.config.app_name, query)
        page = self._service_method(data, "MappedReactionQueryService", "list_mapped_reactions")
        page_info = page.get("page", {})
        selected_count = self._nonnegative_int(
            page_info.get("total") if isinstance(page_info, Mapping) else None
        )
        return ReactionFilterReceipt(
            project_id=request.project_id,
            selected_count=selected_count,
            reaction_smarts=request.reaction_smarts,
            metadata={
                "transport": "mcp",
                "mcp_tool": "compose_query",
                "mcp_app_name": self.config.app_name,
                "mcp_operation": "MappedReactionQueryService.list_mapped_reactions",
                "reaction_smarts": request.reaction_smarts,
                "cleaned_only": request.cleaned_only,
                "query": query,
            },
        )

    def export_rits_dataset(self, request: RitsDatasetExportRequest) -> ReactionSpaceExportReceipt:
        summaries, page_count = self._iter_mapped_reactions(
            request.project_id,
            request.reaction_smarts,
        )
        destination = request.destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        row_count = 0
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                writer = csv.writer(handle, lineterminator="\n")
                writer.writerow(("mapped_reaction_smiles", "ts_structure"))
                for summary in summaries:
                    mapped_reaction_id = self._required_identifier(
                        summary, "mapped reaction", ("id", "mapped_reaction_id")
                    )
                    detail = self._query_mapped_reaction_detail(
                        request.project_id,
                        mapped_reaction_id,
                    )
                    geometry_id = self._transition_state_geometry_id(detail)
                    geometry = self._query_geometry(request.project_id, geometry_id)
                    if request.structure_format != "xyz":
                        raise InvalidRequestError(
                            "MCP RitS export currently supports structure_format=xyz only"
                        )
                    writer.writerow(
                        (
                            self._required_text(
                                detail.get("mapped_reaction_smiles")
                                or summary.get("mapped_reaction_smiles"),
                                "mapped_reaction_smiles",
                            ),
                            self._geometry_xyz(geometry, geometry_id),
                        )
                    )
                    row_count += 1
            assert temporary_path is not None
            os.replace(temporary_path, destination)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        calls = (
            self._mcp_descriptor(
                "MappedReactionQueryService.list_mapped_reactions",
                request.project_id,
                request.reaction_smarts,
                page_count=page_count,
            ),
            self._mcp_descriptor(
                "MappedReactionQueryService.get_mapped_reaction",
                request.project_id,
                request.reaction_smarts,
                row_count=row_count,
            ),
            self._mcp_descriptor(
                "GeometryQueryService.get_geometry",
                request.project_id,
                request.reaction_smarts,
                row_count=row_count,
            ),
        )
        return ReactionSpaceExportReceipt(
            destination=destination,
            row_count=row_count,
            api_contract=RITS_EXPORT_API_CONTRACT,
            mcp_tool_calls=calls,
            metadata={
                "transport": "mcp",
                "mcp_tool": "compose_query",
                "mcp_app_name": self.config.app_name,
                "structure_format": request.structure_format,
                "database_authority": "mapped-reaction-and-geometry-query-services",
            },
        )

    def export_rxnresid_dataset(
        self,
        request: RxnResidDatasetExportRequest,
    ) -> ReactionSpaceExportReceipt:
        summaries, page_count = self._iter_mapped_reactions(
            request.project_id,
            request.reaction_smarts,
        )
        rows: list[tuple[object, ...]] = []
        for summary in summaries:
            mapped_reaction_id = self._required_identifier(
                summary, "mapped reaction", ("id", "mapped_reaction_id")
            )
            thermodynamics = self._query_thermodynamics(
                request.project_id,
                mapped_reaction_id,
            )
            profiles = thermodynamics.get("profiles")
            if not isinstance(profiles, list) or not profiles:
                raise ReactionDatabaseMcpError(
                    f"mapped reaction {mapped_reaction_id} has no materialized thermodynamic profile",
                    tool_name="compose_query",
                )
            profile = profiles[0]
            if not isinstance(profile, Mapping):
                raise ReactionDatabaseMcpError(
                    f"mapped reaction {mapped_reaction_id} returned an invalid thermodynamic profile",
                    tool_name="compose_query",
                )
            reaction = profile.get("reaction", {})
            activation = profile.get("activation", {})
            if not isinstance(reaction, Mapping) or not isinstance(activation, Mapping):
                raise ReactionDatabaseMcpError(
                    f"mapped reaction {mapped_reaction_id} returned incomplete thermodynamics",
                    tool_name="compose_query",
                )
            rows.append(
                (
                    self._required_text(
                        summary.get("mapped_reaction_smiles"),
                        "mapped_reaction_smiles",
                    ),
                    reaction.get("gibbs_free_energy_kcal_mol"),
                    activation.get("gibbs_free_energy_kcal_mol"),
                    profile.get("reactants_running_time_seconds"),
                    profile.get("transition_state_running_time_seconds"),
                    profile.get("products_running_time_seconds"),
                )
            )
        destination = request.destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                writer = csv.writer(handle, lineterminator="\n")
                writer.writerow(
                    (
                        "mapped_reaction_smiles",
                        "reaction_free_energy",
                        "activation_free_energy",
                        "reactant_compute_seconds",
                        "transition_state_compute_seconds",
                        "product_compute_seconds",
                    )
                )
                writer.writerows(rows)
            assert temporary_path is not None
            os.replace(temporary_path, destination)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        calls = (
            self._mcp_descriptor(
                "MappedReactionQueryService.list_mapped_reactions",
                request.project_id,
                request.reaction_smarts,
                page_count=page_count,
            ),
            self._mcp_descriptor(
                "ReactionEnergyQueryService.get_mapped_reaction_thermodynamics",
                request.project_id,
                request.reaction_smarts,
                row_count=len(rows),
            ),
        )
        return ReactionSpaceExportReceipt(
            destination=destination,
            row_count=len(rows),
            api_contract=RXNRESID_EXPORT_API_CONTRACT,
            mcp_tool_calls=calls,
            metadata={
                "transport": "mcp",
                "mcp_tool": "compose_query",
                "mcp_app_name": self.config.app_name,
                "energy_unit": request.energy_unit,
                "timing_unit": request.timing_unit,
                "database_authority": "mapped-reaction-and-thermodynamics-query-services",
            },
        )

    def _build_upload_files(
        self,
        manifest: CalculationLogBatchManifest,
    ) -> tuple[tuple[_UploadFile, ...], str]:
        files: list[_UploadFile] = []
        archive_digest = sha256()
        for path in manifest.files:
            relative_path = path.relative_to(manifest.root).as_posix()
            size_bytes = path.stat().st_size
            file_sha256 = self._file_sha256(path, size_bytes)
            archive_digest.update(relative_path.encode("utf-8"))
            archive_digest.update(b"\0")
            archive_digest.update(file_sha256.encode("ascii"))
            archive_digest.update(b"\0")
            archive_digest.update(str(size_bytes).encode("ascii"))
            archive_digest.update(b"\n")
            files.append(
                _UploadFile(
                    path=path,
                    relative_path=relative_path,
                    size_bytes=size_bytes,
                    file_sha256=file_sha256,
                )
            )
        return tuple(files), archive_digest.hexdigest()

    @staticmethod
    def _file_sha256(path: Path, expected_size: int) -> str:
        digest = sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        if path.stat().st_size != expected_size:
            raise InvalidRequestError(f"calculation log changed while hashing: {path}")
        return digest.hexdigest()

    @staticmethod
    def _read_file_payload(upload_file: _UploadFile) -> bytes:
        with upload_file.path.open("rb") as handle:
            initial_size = os.fstat(handle.fileno()).st_size
            if initial_size != upload_file.size_bytes:
                raise InvalidRequestError(
                    f"calculation log changed while preparing upload: {upload_file.path}"
                )
            payload = handle.read()
            final_size = os.fstat(handle.fileno()).st_size
        if len(payload) != upload_file.size_bytes or final_size != upload_file.size_bytes:
            raise InvalidRequestError(
                f"calculation log changed while reading: {upload_file.path}"
            )
        actual_sha256 = sha256(payload).hexdigest()
        if actual_sha256 != upload_file.file_sha256:
            raise InvalidRequestError(
                f"calculation log changed after hashing: {upload_file.path}"
            )
        return payload

    @staticmethod
    def _is_file_upload_error(error: ReactionDatabaseMcpError) -> bool:
        return error.tool_name == "upload_calculation_log" and error.status_code is None

    def _iter_mapped_reactions(
        self,
        project_id: str,
        reaction_smarts: str,
    ) -> tuple[list[Mapping[str, Any]], int]:
        offset = 0
        page_count = 0
        result: list[Mapping[str, Any]] = []
        while True:
            query = self._mapped_reaction_list_query(
                project_id,
                reaction_smarts,
                limit=self.config.page_size,
                offset=offset,
            )
            data = self._transport.compose_query(self.config.app_name, query)
            page = self._service_method(data, "MappedReactionQueryService", "list_mapped_reactions")
            items = page.get("items", [])
            if not isinstance(items, list):
                raise ReactionDatabaseMcpError("mapped reaction MCP result items must be a list")
            result.extend(item for item in items if isinstance(item, Mapping))
            page_count += 1
            if not items or len(items) < self.config.page_size:
                break
            page_info = page.get("page", {})
            total = self._nonnegative_int(
                page_info.get("total") if isinstance(page_info, Mapping) else None
            )
            offset += len(items)
            if total and offset >= total:
                break
        return result, page_count

    def _query_mapped_reaction_detail(
        self,
        project_id: str,
        mapped_reaction_id: str,
    ) -> Mapping[str, Any]:
        data = self._transport.compose_query(
            self.config.app_name,
            self._mapped_reaction_detail_query(project_id, mapped_reaction_id),
        )
        detail = self._service_method(data, "MappedReactionQueryService", "get_mapped_reaction")
        if not detail:
            raise ReactionDatabaseMcpError(
                f"mapped reaction {mapped_reaction_id} was not returned by the database"
            )
        return detail

    def _query_geometry(self, project_id: str, geometry_id: str) -> Mapping[str, Any]:
        data = self._transport.compose_query(
            self.config.app_name,
            self._geometry_query(project_id, geometry_id),
        )
        geometry = self._service_method(data, "GeometryQueryService", "get_geometry")
        if not geometry:
            raise ReactionDatabaseMcpError(f"geometry {geometry_id} was not returned by the database")
        return geometry

    def _query_thermodynamics(
        self,
        project_id: str,
        mapped_reaction_id: str,
    ) -> Mapping[str, Any]:
        data = self._transport.compose_query(
            self.config.app_name,
            self._thermodynamics_query(project_id, mapped_reaction_id),
        )
        return self._service_method(
            data,
            "ReactionEnergyQueryService",
            "get_mapped_reaction_thermodynamics",
        )

    @staticmethod
    def _service_method(
        data: Mapping[str, Any],
        service_name: str,
        method_name: str,
    ) -> Mapping[str, Any]:
        service = data.get(service_name)
        if not isinstance(service, Mapping):
            raise ReactionDatabaseMcpError(f"MCP result has no {service_name} service data")
        result = service.get(method_name)
        if not isinstance(result, Mapping):
            raise ReactionDatabaseMcpError(
                f"MCP result has no {service_name}.{method_name} data"
            )
        return result

    @staticmethod
    def _mapped_reaction_list_query(
        project_id: str,
        reaction_smarts: str,
        *,
        limit: int,
        offset: int,
    ) -> str:
        return (
            "{ MappedReactionQueryService { "
            "list_mapped_reactions("
            f"project_id: {json.dumps(project_id)} "
            f"reaction_smarts: {json.dumps(reaction_smarts, ensure_ascii=False)} "
            f"limit: {limit} offset: {offset}"
            ") { items { id mapped_reaction_smiles } "
            "page { total limit offset next_cursor } } } }"
        )

    @staticmethod
    def _mapped_reaction_detail_query(project_id: str, mapped_reaction_id: str) -> str:
        return (
            "{ MappedReactionQueryService { get_mapped_reaction("
            f"project_id: {json.dumps(project_id)} "
            f"mapped_reaction_id: {json.dumps(mapped_reaction_id)}"
            ") { id mapped_reaction_smiles "
            "nodes { id role geometries { geometry_id is_primary } } "
            "edges { transition_state_node_id } } } }"
        )

    @staticmethod
    def _geometry_query(project_id: str, geometry_id: str) -> str:
        return (
            "{ GeometryQueryService { get_geometry("
            f"project_id: {json.dumps(project_id)} "
            f"geometry_id: {json.dumps(geometry_id)}"
            ") { id is_transition_state coordinates { atom_index element "
            "x_angstrom y_angstrom z_angstrom } } } }"
        )

    @staticmethod
    def _thermodynamics_query(project_id: str, mapped_reaction_id: str) -> str:
        return (
            "{ ReactionEnergyQueryService { get_mapped_reaction_thermodynamics("
            f"project_id: {json.dumps(project_id)} "
            f"mapped_reaction_id: {json.dumps(mapped_reaction_id)}"
            ") { mapped_reaction_id profiles { level_of_theory "
            "reactants { gibbs_free_energy_hartree } "
            "transition_state { gibbs_free_energy_hartree } "
            "products { gibbs_free_energy_hartree } "
            "activation { gibbs_free_energy_kcal_mol } "
            "reaction { gibbs_free_energy_kcal_mol } "
            "reactants_running_time_seconds transition_state_running_time_seconds "
            "products_running_time_seconds total_running_time_seconds } } } }"
        )

    def _mcp_descriptor(
        self,
        operation: str,
        project_id: str,
        reaction_smarts: str,
        **extra: object,
    ) -> ReactionDatabaseMcpToolCall:
        return ReactionDatabaseMcpToolCall(
            "compose_query",
            {
                "app_name": self.config.app_name,
                "operation": operation,
                "project_id": project_id,
                "reaction_smarts": reaction_smarts,
                **extra,
            },
        )

    @staticmethod
    def _transition_state_geometry_id(detail: Mapping[str, Any]) -> str:
        transition_state_node_ids = {
            str(edge.get("transition_state_node_id"))
            for edge in detail.get("edges", [])
            if isinstance(edge, Mapping) and edge.get("transition_state_node_id")
        }
        candidates: list[tuple[int, str]] = []
        for node in detail.get("nodes", []):
            if not isinstance(node, Mapping):
                continue
            node_id = str(node.get("id", ""))
            role = str(node.get("role", "")).casefold().replace("-", "_").replace(" ", "_")
            node_is_ts = node_id in transition_state_node_ids or role in {
                "ts",
                "transition_state",
                "transitionstate",
            }
            for geometry in node.get("geometries", []):
                if not isinstance(geometry, Mapping) or not geometry.get("geometry_id"):
                    continue
                score = 8 if node_is_ts else 0
                if geometry.get("is_primary"):
                    score += 2
                candidates.append((score, str(geometry["geometry_id"])))
        if not candidates:
            raise ReactionDatabaseMcpError("mapped reaction has no transition-state geometry")
        candidates.sort(reverse=True)
        if candidates[0][0] == 0:
            raise ReactionDatabaseMcpError(
                "mapped reaction has geometries but none is identified as a transition state"
            )
        return candidates[0][1]

    @staticmethod
    def _geometry_xyz(geometry: Mapping[str, Any], geometry_id: str) -> str:
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or not coordinates:
            raise ReactionDatabaseMcpError(f"geometry {geometry_id} has no coordinates")
        rows: list[tuple[int, str, object, object, object]] = []
        for coordinate in coordinates:
            if not isinstance(coordinate, Mapping):
                raise ReactionDatabaseMcpError(f"geometry {geometry_id} returned invalid coordinates")
            try:
                atom_index = int(coordinate["atom_index"])
                element = str(coordinate["element"])
                x = coordinate["x_angstrom"]
                y = coordinate["y_angstrom"]
                z = coordinate["z_angstrom"]
            except (KeyError, TypeError, ValueError) as exc:
                raise ReactionDatabaseMcpError(
                    f"geometry {geometry_id} returned incomplete coordinates"
                ) from exc
            rows.append((atom_index, element, x, y, z))
        rows.sort(key=lambda row: row[0])
        lines = [str(len(rows)), f"geometry_id={geometry_id}"]
        lines.extend(f"{element} {x} {y} {z}" for _, element, x, y, z in rows)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _required_identifier(
        payload: Mapping[str, Any],
        resource: str,
        keys: tuple[str, ...],
    ) -> str:
        for key in keys:
            value = payload.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        for value in payload.values():
            if isinstance(value, Mapping):
                try:
                    return McpReactionDatabaseAdapter._required_identifier(
                        value, resource, keys
                    )
                except ReactionDatabaseMcpError:
                    continue
        raise ReactionDatabaseMcpError(f"database {resource} response has no identifier")

    @staticmethod
    def _required_text(value: object, field_name: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ReactionDatabaseMcpError(f"database response has no {field_name}")
        return text

    @staticmethod
    def _status_value(payload: Mapping[str, Any]) -> str:
        for key in ("ingestion_status", "status", "state", "job_status", "import_status"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.casefold().strip().replace("-", "_")
        return ""

    @classmethod
    def _find_optional_count(cls, payload: Mapping[str, Any], *keys: str) -> int | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (int, float, str)) and not isinstance(value, bool):
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    pass
        for value in payload.values():
            if isinstance(value, Mapping):
                found = cls._find_optional_count(value, *keys)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _nonnegative_int(value: object) -> int:
        if isinstance(value, bool):
            return int(value)
        if not isinstance(value, (int, float, str)):
            return 0
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0


def create_reaction_database_adapter(
    env_file: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> McpReactionDatabaseAdapter:
    """Compose the production MCP adapter for the live application path."""

    return McpReactionDatabaseAdapter.from_env(env_file, environ=environ)


__all__ = [
    "McpReactionDatabaseAdapter",
    "ReactionDatabaseMcpConfig",
    "ReactionDatabaseMcpError",
    "create_reaction_database_adapter",
]
