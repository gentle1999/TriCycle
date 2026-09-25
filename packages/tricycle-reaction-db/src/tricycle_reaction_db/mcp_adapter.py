"""MCP adapter for the project-scoped reaction database.

The database exposes a Streamable HTTP MCP endpoint and a durable batch upload
API.  This module contains a small synchronous MCP/HTTP client so the domain
package can keep its existing ``ReactionDatabaseAdapter`` boundary without
depending on a vendor SDK.  The adapter never relies on a shared filesystem
between the agent and database.
"""

from __future__ import annotations

import base64
import csv
import json
import math
import os
import ssl
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from hashlib import sha256
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
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
_DEFAULT_UPLOAD_MODE = "batch"
_UPLOAD_MODES = frozenset({"batch", "mcp"})
_BATCH_MAX_FILES = 64
_BATCH_MAX_BYTES = 64 * 1024 * 1024
_RITS_MCP_GEOMETRY_PAGE_SIZE = 64
_RXNRESID_THERMO_ALIAS_BATCH_SIZE = 24
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
        "TRICYCLE_REACTION_DB_MCP_TOKEN",
        "TRICYCLE_REACTION_DB_REST_TOKEN",
        "TRICYCLE_REACTION_DB_ORGANIZATION_ID",
        "TRICYCLE_REACTION_DB_MCP_APP_NAME",
        "TRICYCLE_REACTION_DB_MCP_VERIFY_TLS",
        "TRICYCLE_REACTION_DB_MCP_CA_BUNDLE",
        "TRICYCLE_REACTION_DB_MCP_TIMEOUT_SECONDS",
        "TRICYCLE_REACTION_DB_MCP_PAGE_SIZE",
        "TRICYCLE_REACTION_DB_MCP_UPLOAD_MODE",
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

    Calculation-log bytes are transferred through the MCP control plane and
    either the durable multipart queue API or the legacy MCP upload tool.  No
    path in the database service's filesystem is part of this configuration.
    """

    endpoint_url: str
    token: str
    organization_id: str | None = None
    app_name: str = _DEFAULT_APP_NAME
    verify_tls: bool = True
    ca_bundle: Path | None = None
    timeout_seconds: float = 60.0
    page_size: int = 200
    upload_mode: str = _DEFAULT_UPLOAD_MODE
    rest_token: str | None = None

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
        rest_token = self.rest_token
        if rest_token is not None:
            rest_token = str(rest_token).strip() or None
            if rest_token is not None and rest_token.startswith("mcp_"):
                raise InvalidRequestError(
                    "TRICYCLE_REACTION_DB_REST_TOKEN must be a REST/OIDC bearer token; "
                    "MCP access tokens are restricted to /mcp/"
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
        upload_mode = str(self.upload_mode).strip().casefold()
        if upload_mode not in _UPLOAD_MODES:
            raise InvalidRequestError(
                "upload_mode must be one of: " + ", ".join(sorted(_UPLOAD_MODES))
            )
        ca_bundle = self.ca_bundle
        if ca_bundle is not None and not isinstance(ca_bundle, Path):
            ca_bundle = Path(str(ca_bundle)).expanduser()
        if self.verify_tls and ca_bundle is not None and not ca_bundle.is_file():
            raise InvalidRequestError(f"database CA bundle does not exist: {ca_bundle}")
        object.__setattr__(self, "endpoint_url", endpoint)
        object.__setattr__(self, "token", token)
        object.__setattr__(self, "rest_token", rest_token)
        object.__setattr__(self, "organization_id", organization_id)
        object.__setattr__(self, "app_name", app_name)
        object.__setattr__(self, "ca_bundle", ca_bundle)
        object.__setattr__(self, "upload_mode", upload_mode)

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
        token = values.get("TRICYCLE_REACTION_DB_MCP_TOKEN", "").strip()
        rest_token = values.get("TRICYCLE_REACTION_DB_REST_TOKEN", "").strip() or None
        if not raw_endpoint:
            raise IntegrationNotConfiguredError(
                "TRICYCLE_REACTION_DB_MCP_URL is required for live database operations"
            )
        if not token:
            raise IntegrationNotConfiguredError(
                "TRICYCLE_REACTION_DB_MCP_TOKEN is required for live database operations"
            )
        ca_bundle_value = values.get("TRICYCLE_REACTION_DB_MCP_CA_BUNDLE", "").strip()
        return cls(
            endpoint_url=raw_endpoint,
            token=token,
            rest_token=rest_token,
            organization_id=values.get("TRICYCLE_REACTION_DB_ORGANIZATION_ID"),
            app_name=values.get("TRICYCLE_REACTION_DB_MCP_APP_NAME", _DEFAULT_APP_NAME),
            verify_tls=_env_bool(
                values,
                "TRICYCLE_REACTION_DB_MCP_VERIFY_TLS",
                True,
            ),
            ca_bundle=Path(ca_bundle_value).expanduser() if ca_bundle_value else None,
            timeout_seconds=_env_float(values, "TRICYCLE_REACTION_DB_MCP_TIMEOUT_SECONDS", 60.0),
            page_size=_env_int(values, "TRICYCLE_REACTION_DB_MCP_PAGE_SIZE", 200),
            upload_mode=values.get("TRICYCLE_REACTION_DB_MCP_UPLOAD_MODE", _DEFAULT_UPLOAD_MODE),
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
                if exc.code in {500, 502, 503, 504} and attempt < _MCP_RATE_LIMIT_RETRIES:
                    time.sleep(min(2.0**attempt, 10.0))
                    continue
                suffix = f": {detail[:500]}" if detail else ""
                raise ReactionDatabaseMcpError(
                    f"database MCP HTTP {exc.code}{suffix}",
                    status_code=exc.code,
                ) from exc
            except (IncompleteRead, RemoteDisconnected, ConnectionResetError) as exc:
                if attempt < _MCP_RATE_LIMIT_RETRIES:
                    time.sleep(min(2.0**attempt, 10.0))
                    continue
                raise ReactionDatabaseMcpError(
                    f"database MCP response was interrupted: {exc}"
                ) from exc
            except URLError as exc:
                if self._is_retryable_url_error(exc) and attempt < _MCP_RATE_LIMIT_RETRIES:
                    time.sleep(min(2.0**attempt, 10.0))
                    continue
                raise ReactionDatabaseMcpError(f"database MCP request failed: {exc}") from exc
            except (TimeoutError, OSError) as exc:
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
    def _is_retryable_url_error(error: URLError) -> bool:
        reason = error.reason
        if isinstance(
            reason,
            (IncompleteRead, RemoteDisconnected, ConnectionResetError, ssl.SSLEOFError),
        ):
            return True
        return "EOF occurred in violation of protocol" in str(reason)

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

    def list_projects(
        self,
        *,
        organization_id: str | None = None,
        include_archived: bool = False,
    ) -> tuple[Mapping[str, Any], ...]:
        """List reaction-database projects through the read-only MCP tool."""

        self._transport.ensure_tool("list_projects")
        resolved_organization_id = organization_id or self.config.organization_id
        arguments: dict[str, object] = {"include_archived": include_archived}
        if resolved_organization_id:
            arguments["organization_id"] = resolved_organization_id
        result = self._transport.call_tool("list_projects", arguments)
        raw_projects = result.get("data")
        if not isinstance(raw_projects, list):
            raw_projects = result.get("projects")
        if not isinstance(raw_projects, list):
            raise ReactionDatabaseMcpError(
                "database list_projects returned no project list",
                tool_name="list_projects",
            )
        return tuple(
            dict(project)
            for project in raw_projects
            if isinstance(project, Mapping)
        )

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
        if self.config.upload_mode == "mcp":
            self._transport.ensure_tool("upload_calculation_log")
        idempotency_key = request.resolved_idempotency_key(manifest)
        file_transfer = (
            "database.upload-batches.multipart"
            if self.config.upload_mode == "batch"
            else "mcp.upload_calculation_log.base64"
        )
        create_arguments = {
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
                "file_transfer": file_transfer,
                "file_count": len(upload_files),
                "total_bytes": manifest.total_bytes,
            },
            "calculation_protocol": {
                "source": request.source,
                "batch_id": manifest.batch_id,
                "provenance": dict(request.provenance),
                "metadata": dict(request.metadata),
            },
        }
        reused_project = False
        try:
            project = self._transport.call_tool("create_project", create_arguments)
        except ReactionDatabaseMcpError as exc:
            if exc.code != "conflict":
                raise
            project = self._find_resumable_project(
                organization_id=organization_id,
                slug=request.project.slug,
                idempotency_key=idempotency_key,
            )
            reused_project = True
        project_id = self._required_identifier(project, "project", ("project_id", "id"))

        existing_keys: set[tuple[str, int]] = set()
        if reused_project:
            existing_keys = self._list_existing_artifact_keys(project_id)
        upload_files_to_submit = [
            upload_file
            for upload_file in upload_files
            if (upload_file.file_sha256, upload_file.size_bytes) not in existing_keys
        ]
        existing_count = len(upload_files) - len(upload_files_to_submit)
        if self.config.upload_mode == "batch":
            return self._ingest_with_batch_upload(
                request=request,
                manifest=manifest,
                project_id=project_id,
                archive_sha256=archive_sha256,
                idempotency_key=idempotency_key,
                upload_files_to_submit=upload_files_to_submit,
                existing_count=existing_count,
                reused_project=reused_project,
            )
        accepted_count = existing_count
        failed_count = 0
        uploaded_count = 0
        queue_item_status_counts: dict[str, int] = {}
        queue_batch_status_counts: dict[str, int] = {}
        queue_submissions: list[dict[str, str]] = []
        errors: list[str] = []
        for upload_file in upload_files_to_submit:
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
            "existing_count": existing_count,
            "uploaded_count": uploaded_count,
            "accepted_count": accepted_count,
            "failed_count": failed_count,
            "queue_batch_count": len(queue_submissions),
            "queue_item_status_counts": queue_item_status_counts,
            "queue_batch_status_counts": queue_batch_status_counts,
            "queue_submissions": queue_submissions,
            "parse_completion": "not_waited",
            "project_reused": reused_project,
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

    def _ingest_with_batch_upload(
        self,
        *,
        request: ReactionBatchImportRequest,
        manifest: CalculationLogBatchManifest,
        project_id: str,
        archive_sha256: str,
        idempotency_key: str,
        upload_files_to_submit: list[_UploadFile],
        existing_count: int,
        reused_project: bool,
    ) -> ReactionDatabaseImportReceipt:
        accepted_count = existing_count
        failed_count = 0
        uploaded_count = 0
        queue_item_status_counts: dict[str, int] = {}
        queue_batch_status_counts: dict[str, int] = {}
        queue_submissions: list[dict[str, str]] = []
        errors: list[str] = []
        queue_batch_count = 0
        upload_groups = self._upload_groups(upload_files_to_submit)
        for group_index, group in enumerate(upload_groups):
            try:
                batch, items, client_ids = self._upload_one_batch(
                    project_id=project_id,
                    request=request,
                    manifest=manifest,
                    archive_sha256=archive_sha256,
                    idempotency_key=idempotency_key,
                    group=group,
                    group_index=group_index,
                    group_count=len(upload_groups),
                )
            except (InvalidRequestError, OSError, ReactionDatabaseMcpError) as exc:
                failed_count += len(group)
                errors.append(f"batch {group_index + 1}: {exc}")
                continue

            batch_id = self._required_identifier(batch, "upload batch", ("id", "batch_id"))
            queue_batch_count += 1
            batch_status = self._status_value(batch) or "unknown"
            queue_batch_status_counts[batch_status] = (
                queue_batch_status_counts.get(batch_status, 0) + 1
            )
            items_by_client_id = {
                str(item.get("client_file_id")): item
                for item in items
                if isinstance(item, Mapping) and item.get("client_file_id")
            }
            uploaded_count += len(items_by_client_id)
            for upload_file in group:
                client_file_id = client_ids[upload_file.relative_path]
                item = items_by_client_id.get(client_file_id)
                if item is None:
                    failed_count += 1
                    errors.append(
                        f"{upload_file.relative_path}: batch response omitted client_file_id"
                    )
                    continue
                item_id = self._required_identifier(
                    item,
                    "upload batch item",
                    ("id", "item_id", "client_file_id"),
                )
                item_status = self._status_value(item) or "unknown"
                queue_item_status_counts[item_status] = (
                    queue_item_status_counts.get(item_status, 0) + 1
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
            "transport": "mcp+http",
            "mcp_endpoint": self.config.endpoint_url,
            "mcp_app_name": self.config.app_name,
            "mcp_tools": ["create_project"],
            "http_endpoints": [
                "POST /api/upload-batches",
                "POST /api/upload-batches/{batch_id}/files",
            ],
            "file_transfer": "multipart-batch-queue",
            "queue_mode": "durable-upload-batch",
            "manifest_digest": manifest.manifest_digest,
            "archive_sha256": archive_sha256,
            "total_count": len(manifest.files),
            "existing_count": existing_count,
            "uploaded_count": uploaded_count,
            "accepted_count": accepted_count,
            "failed_count": failed_count,
            "queue_batch_count": queue_batch_count,
            "queue_item_status_counts": queue_item_status_counts,
            "queue_batch_status_counts": queue_batch_status_counts,
            "queue_submissions": queue_submissions,
            "parse_completion": "not_waited",
            "project_reused": reused_project,
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

    @staticmethod
    def _upload_groups(upload_files: list[_UploadFile]) -> tuple[tuple[_UploadFile, ...], ...]:
        groups: list[tuple[_UploadFile, ...]] = []
        current: list[_UploadFile] = []
        current_bytes = 0
        for upload_file in upload_files:
            if current and (
                len(current) >= _BATCH_MAX_FILES
                or current_bytes + upload_file.size_bytes > _BATCH_MAX_BYTES
            ):
                groups.append(tuple(current))
                current = []
                current_bytes = 0
            current.append(upload_file)
            current_bytes += upload_file.size_bytes
        if current:
            groups.append(tuple(current))
        return tuple(groups)

    def _upload_one_batch(
        self,
        *,
        project_id: str,
        request: ReactionBatchImportRequest,
        manifest: CalculationLogBatchManifest,
        archive_sha256: str,
        idempotency_key: str,
        group: tuple[_UploadFile, ...],
        group_index: int,
        group_count: int,
    ) -> tuple[Mapping[str, Any], list[Mapping[str, Any]], dict[str, str]]:
        payloads: list[tuple[_UploadFile, bytes]] = []
        batch_digest = sha256()
        for upload_file in group:
            payload = self._read_file_payload(upload_file)
            payloads.append((upload_file, payload))
            batch_digest.update(upload_file.relative_path.encode("utf-8"))
            batch_digest.update(b"\0")
            batch_digest.update(upload_file.file_sha256.encode("ascii"))
            batch_digest.update(b"\0")
            batch_digest.update(str(upload_file.size_bytes).encode("ascii"))
            batch_digest.update(b"\n")
        batch_manifest_sha256 = batch_digest.hexdigest()
        client_ids = {
            upload_file.relative_path: str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{idempotency_key}:{batch_manifest_sha256}:{upload_file.relative_path}",
                )
            )
            for upload_file, _payload in payloads
        }
        create_payload = {
            "project_id": project_id,
            "artifact_kind": "calculation_output",
            "shared_metadata": {
                "adapter": "tricycle-reaction-db.mcp",
                "idempotency_key": idempotency_key,
                "source": request.source,
                "batch_id": manifest.batch_id,
                "manifest_digest": manifest.manifest_digest,
                "archive_sha256": archive_sha256,
                "chunk_index": group_index,
                "chunk_count": group_count,
                "chunk_manifest_sha256": batch_manifest_sha256,
                "file_transfer": "database.upload-batches.multipart",
            },
            "files": [
                {
                    "client_file_id": client_ids[upload_file.relative_path],
                    "original_filename": upload_file.path.name,
                    "relative_path": upload_file.relative_path,
                    "size_bytes": upload_file.size_bytes,
                    "media_type": "text/plain",
                }
                for upload_file, _payload in payloads
            ],
        }
        batch = self._rest_json("POST", "/api/upload-batches", create_payload)
        if not isinstance(batch, Mapping):
            raise ReactionDatabaseMcpError("batch upload creation returned no batch")
        batch_id = self._required_identifier(batch, "upload batch", ("id", "batch_id"))
        body, content_type = self._multipart_body(payloads, client_ids)
        raw_response, _response_headers = self._rest_request(
            "POST",
            f"/api/upload-batches/{batch_id}/files",
            body,
            content_type=content_type,
        )
        try:
            raw_items = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReactionDatabaseMcpError("batch file upload returned invalid JSON") from exc
        if not isinstance(raw_items, list):
            raise ReactionDatabaseMcpError("batch file upload returned no item list")
        items = [item for item in raw_items if isinstance(item, Mapping)]
        return batch, items, client_ids

    def _multipart_body(
        self,
        payloads: list[tuple[_UploadFile, bytes]],
        client_ids: Mapping[str, str],
    ) -> tuple[bytes, str]:
        boundary = f"tricycle-{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        for upload_file, _payload in payloads:
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    b'Content-Disposition: form-data; name="client_file_ids"\r\n\r\n',
                    client_ids[upload_file.relative_path].encode("ascii"),
                    b"\r\n",
                ]
            )
        for upload_file, payload in payloads:
            filename = upload_file.path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    (
                        'Content-Disposition: form-data; name="files"; '
                        f'filename="{filename}"\r\n'
                    ).encode(),
                    b"Content-Type: text/plain\r\n\r\n",
                    payload,
                    b"\r\n",
                ]
            )
        chunks.append(f"--{boundary}--\r\n".encode("ascii"))
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def _rest_url(self, path: str) -> str:
        endpoint = urlsplit(self.config.endpoint_url)
        requested = urlsplit(path)
        base_path = endpoint.path.rstrip("/")
        if base_path.endswith("/mcp"):
            base_path = base_path[:-4]
        joined_path = f"{base_path.rstrip('/')}/{requested.path.lstrip('/')}"
        return urlunsplit(
            (endpoint.scheme, endpoint.netloc, joined_path, requested.query, "")
        )

    def _require_rest_token(self) -> str:
        token = self.config.rest_token
        if token is None:
            raise IntegrationNotConfiguredError(
                "TRICYCLE_REACTION_DB_REST_TOKEN is required for REST API operations; "
                "a TRICYCLE_REACTION_DB_MCP_TOKEN cannot authenticate REST requests"
            )
        return token

    def _rest_json(self, method: str, path: str, payload: object | None = None) -> Any:
        body = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        response, _headers = self._rest_request(
            method,
            path,
            body,
            content_type="application/json" if body is not None else None,
        )
        try:
            return json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReactionDatabaseMcpError(f"database REST {method} {path} returned invalid JSON") from exc

    def _rest_request(
        self,
        method: str,
        path: str,
        data: bytes | None,
        *,
        content_type: str | None,
    ) -> tuple[bytes, Mapping[str, str]]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._require_rest_token()}",
            "User-Agent": "TriCycle-reaction-db-upload-batch/0.1",
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        for attempt in range(_MCP_RATE_LIMIT_RETRIES + 1):
            request = Request(self._rest_url(path), data=data, headers=headers, method=method)
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
                    return raw, response_headers
            except HTTPError as exc:
                detail = exc.read(2048).decode("utf-8", errors="replace").strip()
                if exc.code == 429 and attempt < _MCP_RATE_LIMIT_RETRIES:
                    time.sleep(_McpTransport._retry_after_seconds(exc.headers, detail))
                    continue
                if exc.code in {500, 502, 503, 504} and attempt < _MCP_RATE_LIMIT_RETRIES:
                    time.sleep(min(2.0**attempt, 10.0))
                    continue
                suffix = f": {detail[:500]}" if detail else ""
                raise ReactionDatabaseMcpError(
                    f"database REST {method} {path} HTTP {exc.code}{suffix}",
                    status_code=exc.code,
                ) from exc
            except (IncompleteRead, RemoteDisconnected, ConnectionResetError) as exc:
                if attempt < _MCP_RATE_LIMIT_RETRIES:
                    time.sleep(min(2.0**attempt, 10.0))
                    continue
                raise ReactionDatabaseMcpError(
                    f"database REST {method} {path} response was interrupted: {exc}"
                ) from exc
            except URLError as exc:
                if _McpTransport._is_retryable_url_error(exc) and attempt < _MCP_RATE_LIMIT_RETRIES:
                    time.sleep(min(2.0**attempt, 10.0))
                    continue
                raise ReactionDatabaseMcpError(
                    f"database REST {method} {path} request failed: {exc}"
                ) from exc
            except (TimeoutError, OSError) as exc:
                raise ReactionDatabaseMcpError(
                    f"database REST {method} {path} request failed: {exc}"
                ) from exc
        raise AssertionError("REST retry loop exited unexpectedly")

    def _find_resumable_project(
        self,
        *,
        organization_id: str,
        slug: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        for project in self.list_projects(
            organization_id=organization_id,
            include_archived=False,
        ):
            if str(project.get("slug", "")).strip() != slug:
                continue
            if str(project.get("organization_id", "")).strip() != organization_id:
                continue
            data_source = project.get("data_source")
            existing_key = (
                str(data_source.get("idempotency_key", "")).strip()
                if isinstance(data_source, Mapping)
                else ""
            )
            if existing_key != idempotency_key:
                raise ReactionDatabaseMcpError(
                    f"project slug {slug!r} already belongs to a different import",
                    code="conflict",
                    tool_name="list_projects",
                )
            return project
        raise ReactionDatabaseMcpError(
            f"project slug {slug!r} conflicted but could not be found for resume",
            code="conflict",
            tool_name="list_projects",
        )

    def _list_existing_artifact_keys(self, project_id: str) -> set[tuple[str, int]]:
        self._transport.ensure_tool("compose_query")
        keys: set[tuple[str, int]] = set()
        offset = 0
        while True:
            query = (
                "{ ArtifactQueryService { list_artifacts("
                f"project_id: {json.dumps(project_id)} "
                f"limit: {self.config.page_size} offset: {offset}"
                ") { items { content_sha256 size_bytes } "
                "page { total limit offset } } } }"
            )
            data = self._transport.compose_query(self.config.app_name, query)
            page_result = self._service_method(
                data,
                "ArtifactQueryService",
                "list_artifacts",
            )
            items = page_result.get("items")
            if not isinstance(items, list):
                raise ReactionDatabaseMcpError(
                    "MCP ArtifactQueryService.list_artifacts returned no items"
                )
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                content_sha256 = str(item.get("content_sha256", "")).strip().casefold()
                size_bytes = self._nonnegative_int(item.get("size_bytes"))
                if content_sha256 and len(content_sha256) == 64:
                    keys.add((content_sha256, size_bytes))
            page = page_result.get("page")
            total = self._nonnegative_int(page.get("total")) if isinstance(page, Mapping) else 0
            offset += len(items)
            if not items or len(items) < self.config.page_size or (total and offset >= total):
                return keys

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
        if request.structure_format != "xyz":
            raise InvalidRequestError(
                "MCP RitS export currently supports structure_format=xyz only"
            )
        destination = request.destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        row_count = 0
        reaction_page_count = 0
        selected_reaction_count = 0
        geometry_page_count = 0
        geometry_record_count = 0
        exported_reactions: set[str] = set()
        selected_reactions: set[str] = set()
        try:
            for summaries in self._iter_mapped_reaction_pages(
                request.project_id,
                request.reaction_smarts,
            ):
                reaction_page_count += 1
                selected_reaction_count += len(summaries)
                for summary in summaries:
                    selected_reactions.add(
                        self._required_text(
                            summary.get("mapped_reaction_smiles"),
                            "mapped_reaction_smiles",
                        )
                    )

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
                for records in self._iter_mapped_reaction_geometry_pages(request.project_id):
                    geometry_page_count += 1
                    geometry_record_count += len(records)
                    for record in records:
                        mapped_smiles, xyz = self._mapped_geometry_record_xyz(record)
                        if mapped_smiles not in selected_reactions:
                            continue
                        writer.writerow((mapped_smiles, xyz))
                        exported_reactions.add(mapped_smiles)
                        row_count += 1
            missing_reactions = selected_reactions - exported_reactions
            if missing_reactions:
                raise ReactionDatabaseMcpError(
                    "mapped-reaction geometry export omitted "
                    f"{len(missing_reactions)} reaction(s) selected by the database reaction_smarts"
                )
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
                page_count=reaction_page_count,
                selected_reaction_count=selected_reaction_count,
            ),
            ReactionDatabaseMcpToolCall(
                "export_mapped_reaction_transition_state_geometries",
                {
                    "project_id": request.project_id,
                    "limit": _RITS_MCP_GEOMETRY_PAGE_SIZE,
                    "page_count": geometry_page_count,
                    "record_count": geometry_record_count,
                    "matched_record_count": row_count,
                },
            ),
        )
        return ReactionSpaceExportReceipt(
            destination=destination,
            row_count=row_count,
            api_contract=RITS_EXPORT_API_CONTRACT,
            mcp_tool_calls=calls,
            metadata={
                "transport": "mcp",
                "mcp_tool": "export_mapped_reaction_transition_state_geometries",
                "mcp_app_name": self.config.app_name,
                "structure_format": request.structure_format,
                "database_authority": "mapped-reaction-query-and-streaming-geometry-export",
                "coordinate_atom_order": "ascending-reaction-atom-map-number",
                "streaming": {
                    "mode": "paged-mcp-cursor",
                    "geometry_page_size": _RITS_MCP_GEOMETRY_PAGE_SIZE,
                    "filtered_reaction_pages": reaction_page_count,
                    "geometry_pages": geometry_page_count,
                    "geometry_records_scanned": geometry_record_count,
                    "selected_reactions": selected_reaction_count,
                    "exported_rows": row_count,
                },
            },
        )

    def export_rxnresid_dataset(
        self,
        request: RxnResidDatasetExportRequest,
    ) -> ReactionSpaceExportReceipt:
        if not request.cleaned_only:
            raise InvalidRequestError("RxnResid thermodynamics export requires cleaned_only=true")

        destination = request.destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        mapped_reaction_pages = 0
        summaries: list[Mapping[str, Any]] = []
        row_count = 0
        batch_count = 0
        excluded_reasons: dict[str, int] = {}
        try:
            for page in self._iter_mapped_reaction_pages(
                request.project_id,
                request.reaction_smarts,
            ):
                mapped_reaction_pages += 1
                for summary in page:
                    self._required_identifier(
                        summary,
                        "mapped reaction",
                        ("id", "mapped_reaction_id"),
                    )
                    self._required_text(
                        summary.get("mapped_reaction_smiles"),
                        "mapped_reaction_smiles",
                    )
                    summaries.append(summary)

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
                for start in range(0, len(summaries), _RXNRESID_THERMO_ALIAS_BATCH_SIZE):
                    batch = summaries[start : start + _RXNRESID_THERMO_ALIAS_BATCH_SIZE]
                    batch_count += 1
                    mapped_reaction_ids = tuple(
                        self._required_identifier(
                            summary,
                            "mapped reaction",
                            ("id", "mapped_reaction_id"),
                        )
                        for summary in batch
                    )
                    data = self._transport.compose_query(
                        self.config.app_name,
                        self._thermodynamics_batch_query(
                            request.project_id,
                            mapped_reaction_ids,
                        ),
                    )
                    service = data.get("ReactionEnergyQueryService")
                    if not isinstance(service, Mapping):
                        raise ReactionDatabaseMcpError(
                            "MCP result has no ReactionEnergyQueryService data"
                        )
                    for index, (summary, mapped_reaction_id) in enumerate(
                        zip(batch, mapped_reaction_ids)
                    ):
                        result = service.get(f"thermodynamics_{index}")
                        if not isinstance(result, Mapping):
                            raise ReactionDatabaseMcpError(
                                "mapped-reaction thermodynamics result is missing or invalid "
                                f"for reaction {mapped_reaction_id}"
                            )
                        returned_id = str(result.get("mapped_reaction_id", "")).strip()
                        if returned_id != mapped_reaction_id:
                            raise ReactionDatabaseMcpError(
                                "mapped-reaction thermodynamics result does not match the "
                                f"requested project-scoped ID {mapped_reaction_id}"
                            )
                        profiles = result.get("profiles")
                        if not isinstance(profiles, list) or not profiles:
                            raise ReactionDatabaseMcpError(
                                "mapped reaction has no materialized thermodynamics profile: "
                                f"{mapped_reaction_id}"
                            )
                        profile = profiles[0]
                        if not isinstance(profile, Mapping):
                            raise ReactionDatabaseMcpError(
                                "mapped reaction has an invalid materialized thermodynamics "
                                f"profile: {mapped_reaction_id}"
                            )
                        exclusion_reason = self._thermodynamics_exclusion_reason(profile)
                        if exclusion_reason is not None:
                            excluded_reasons[exclusion_reason] = (
                                excluded_reasons.get(exclusion_reason, 0) + 1
                            )
                            continue

                        reaction_profile = profile["reaction"]
                        activation_profile = profile["activation"]
                        writer.writerow(
                            (
                                self._required_text(
                                    summary.get("mapped_reaction_smiles"),
                                    "mapped_reaction_smiles",
                                ),
                                self._profile_number(
                                    reaction_profile["gibbs_free_energy_kcal_mol"],
                                    "reaction.gibbs_free_energy_kcal_mol",
                                ),
                                self._profile_number(
                                    activation_profile["gibbs_free_energy_kcal_mol"],
                                    "activation.gibbs_free_energy_kcal_mol",
                                ),
                                self._optional_profile_number(
                                    profile.get("reactants_running_time_seconds"),
                                    "reactants_running_time_seconds",
                                ),
                                self._optional_profile_number(
                                    profile.get("transition_state_running_time_seconds"),
                                    "transition_state_running_time_seconds",
                                ),
                                self._optional_profile_number(
                                    profile.get("products_running_time_seconds"),
                                    "products_running_time_seconds",
                                ),
                            )
                        )
                        row_count += 1
            assert temporary_path is not None
            os.replace(temporary_path, destination)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

        excluded_count = sum(excluded_reasons.values())
        filter_call = self._mcp_descriptor(
            "MappedReactionQueryService.list_mapped_reactions",
            request.project_id,
            request.reaction_smarts,
            cleaned_only=True,
            page_count=mapped_reaction_pages,
            page_size=self.config.page_size,
            mapped_reaction_count=len(summaries),
        )
        thermo_call = self._mcp_descriptor(
            "ReactionEnergyQueryService.get_mapped_reaction_thermodynamics",
            request.project_id,
            request.reaction_smarts,
            batch_count=batch_count,
            queried_reaction_count=len(summaries),
            max_aliases_per_query=_RXNRESID_THERMO_ALIAS_BATCH_SIZE,
            profile_policy="profiles[0]",
        )
        receipt_path = destination.parent / "receipt.json"
        receipt = ReactionSpaceExportReceipt(
            destination=destination,
            row_count=row_count,
            api_contract=RXNRESID_EXPORT_API_CONTRACT,
            mcp_tool_calls=(filter_call, thermo_call),
            metadata={
                "transport": "mcp",
                "mcp_app_name": self.config.app_name,
                "project_id": request.project_id,
                "reaction_smarts": request.reaction_smarts,
                "cleaned_only": request.cleaned_only,
                "energy_unit": "kcal/mol",
                "timing_unit": "seconds",
                "mapped_reaction_pages": mapped_reaction_pages,
                "mapped_reaction_count": len(summaries),
                "exported_rows": row_count,
                "excluded_incomplete_thermodynamics_count": excluded_count,
                "excluded_reasons": excluded_reasons,
                "thermodynamics_profile_policy": "profiles[0]",
                "database_authority": "mapped-reaction-query-and-thermodynamics-materialization",
                "receipt_path": str(receipt_path),
            },
        )
        receipt_temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=receipt_path.parent,
                prefix=f".{receipt_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                receipt_temporary_path = Path(handle.name)
                json.dump(receipt.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            assert receipt_temporary_path is not None
            os.replace(receipt_temporary_path, receipt_path)
        except Exception:
            if receipt_temporary_path is not None:
                receipt_temporary_path.unlink(missing_ok=True)
            raise
        return receipt

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

    def _iter_mapped_reaction_pages(
        self,
        project_id: str,
        reaction_smarts: str,
    ) -> Iterator[tuple[Mapping[str, Any], ...]]:
        offset = 0
        while True:
            query = self._mapped_reaction_list_query(
                project_id,
                reaction_smarts,
                limit=self.config.page_size,
                offset=offset,
            )
            data = self._transport.compose_query(self.config.app_name, query)
            page = self._service_method(data, "MappedReactionQueryService", "list_mapped_reactions")
            items = page.get("items")
            if not isinstance(items, list):
                raise ReactionDatabaseMcpError("mapped reaction MCP result items must be a list")
            summaries = tuple(item for item in items if isinstance(item, Mapping))
            if len(summaries) != len(items):
                raise ReactionDatabaseMcpError(
                    "mapped reaction MCP result contains an invalid summary"
                )
            page_info = page.get("page")
            if not isinstance(page_info, Mapping):
                raise ReactionDatabaseMcpError("mapped reaction MCP result has no page metadata")
            returned_offset = self._nonnegative_int(page_info.get("offset"))
            if returned_offset != offset:
                raise ReactionDatabaseMcpError(
                    "mapped reaction MCP pagination returned an unexpected offset"
                )
            total = self._nonnegative_int(page_info.get("total"))
            yield summaries
            if not items or (total and offset + len(items) >= total):
                break
            if len(items) < self.config.page_size and not total:
                break
            offset += len(items)

    def _iter_mapped_reaction_geometry_pages(
        self,
        project_id: str,
    ) -> Iterator[tuple[Mapping[str, Any], ...]]:
        tool_name = "export_mapped_reaction_transition_state_geometries"
        self._transport.ensure_tool(tool_name)
        after_binding_id: str | None = None
        while True:
            page = self._transport.call_tool(
                tool_name,
                {
                    "project_id": project_id,
                    "after_binding_id": after_binding_id,
                    "limit": _RITS_MCP_GEOMETRY_PAGE_SIZE,
                },
            )
            returned_project_id = str(page.get("project_id", "")).strip()
            if returned_project_id != project_id:
                raise ReactionDatabaseMcpError(
                    "mapped-reaction geometry MCP page does not match the requested project_id",
                    tool_name=tool_name,
                )
            raw_records = page.get("records")
            if not isinstance(raw_records, list):
                raise ReactionDatabaseMcpError(
                    "mapped-reaction geometry MCP page has no records list",
                    tool_name=tool_name,
                )
            if len(raw_records) > _RITS_MCP_GEOMETRY_PAGE_SIZE:
                raise ReactionDatabaseMcpError(
                    "mapped-reaction geometry MCP page exceeded the requested limit",
                    tool_name=tool_name,
                )
            records = tuple(record for record in raw_records if isinstance(record, Mapping))
            if len(records) != len(raw_records):
                raise ReactionDatabaseMcpError(
                    "mapped-reaction geometry MCP page contains an invalid record",
                    tool_name=tool_name,
                )
            has_more = page.get("has_more")
            if not isinstance(has_more, bool):
                raise ReactionDatabaseMcpError(
                    "mapped-reaction geometry MCP page has no boolean has_more field",
                    tool_name=tool_name,
                )
            next_after_binding_id = page.get("next_after_binding_id")
            if has_more:
                next_cursor = self._required_text(
                    next_after_binding_id,
                    "next_after_binding_id",
                )
                if next_cursor == after_binding_id or not records:
                    raise ReactionDatabaseMcpError(
                        "mapped-reaction geometry MCP pagination did not advance",
                        tool_name=tool_name,
                    )
            else:
                next_cursor = None
            yield records
            if not has_more:
                break
            after_binding_id = next_cursor

    @classmethod
    def _mapped_geometry_record_xyz(
        cls,
        record: Mapping[str, Any],
    ) -> tuple[str, str]:
        if record.get("schema") not in {
            "mapped-reaction-ts-geometry-v1",
            "mapped-reaction-ts-geometry-v2",
        }:
            raise ReactionDatabaseMcpError(
                "mapped-reaction geometry MCP record has an unsupported schema",
                tool_name="export_mapped_reaction_transition_state_geometries",
            )
        mapped_smiles = cls._required_text(record.get("key"), "mapped reaction SMILES")
        value = record.get("value")
        geometry = value.get("geometry") if isinstance(value, Mapping) else None
        if not isinstance(geometry, Mapping):
            raise ReactionDatabaseMcpError(
                "mapped-reaction geometry MCP record has no geometry object",
                tool_name="export_mapped_reaction_transition_state_geometries",
            )
        geometry_id = cls._required_text(geometry.get("geometry_id"), "geometry_id")
        if str(geometry.get("coordinate_units", "")).casefold() != "angstrom":
            raise ReactionDatabaseMcpError(
                f"geometry {geometry_id} coordinates must be in angstrom",
                tool_name="export_mapped_reaction_transition_state_geometries",
            )
        atoms = geometry.get("atoms")
        if not isinstance(atoms, list) or not atoms:
            raise ReactionDatabaseMcpError(
                f"geometry {geometry_id} has no atom coordinates",
                tool_name="export_mapped_reaction_transition_state_geometries",
            )
        mapped_atoms: list[tuple[int, str, float, float, float]] = []
        for atom in atoms:
            if not isinstance(atom, Mapping):
                raise ReactionDatabaseMcpError(
                    f"geometry {geometry_id} returned an invalid atom",
                    tool_name="export_mapped_reaction_transition_state_geometries",
                )
            map_number = atom.get("atom_map_number")
            element = cls._required_text(atom.get("element"), "atom element")
            coordinates = atom.get("coordinates_angstrom")
            if (
                isinstance(map_number, bool)
                or not isinstance(map_number, int)
                or map_number < 1
                or not isinstance(coordinates, list)
                or len(coordinates) != 3
            ):
                raise ReactionDatabaseMcpError(
                    f"geometry {geometry_id} returned incomplete mapped coordinates",
                    tool_name="export_mapped_reaction_transition_state_geometries",
                )
            numeric_coordinates: list[float] = []
            for coordinate in coordinates:
                if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
                    raise ReactionDatabaseMcpError(
                        f"geometry {geometry_id} returned non-numeric coordinates",
                        tool_name="export_mapped_reaction_transition_state_geometries",
                    )
                converted = float(coordinate)
                if not math.isfinite(converted):
                    raise ReactionDatabaseMcpError(
                        f"geometry {geometry_id} returned non-finite coordinates",
                        tool_name="export_mapped_reaction_transition_state_geometries",
                    )
                numeric_coordinates.append(converted)
            mapped_atoms.append(
                (
                    map_number,
                    element,
                    numeric_coordinates[0],
                    numeric_coordinates[1],
                    numeric_coordinates[2],
                )
            )
        mapped_atoms.sort(key=lambda atom: atom[0])
        expected_map_numbers = list(range(1, len(mapped_atoms) + 1))
        actual_map_numbers = [atom[0] for atom in mapped_atoms]
        if actual_map_numbers != expected_map_numbers:
            raise ReactionDatabaseMcpError(
                f"geometry {geometry_id} atom-map numbers must be the complete 1-based sequence",
                tool_name="export_mapped_reaction_transition_state_geometries",
            )
        binding_id = str(geometry.get("geometry_binding_id", "")).strip()
        comment = f"geometry_id={geometry_id}"
        if binding_id:
            comment += f" geometry_binding_id={binding_id}"
        lines = [str(len(mapped_atoms)), comment]
        lines.extend(
            f"{element} {x:.15g} {y:.15g} {z:.15g}"
            for _map_number, element, x, y, z in mapped_atoms
        )
        return mapped_smiles, "\n".join(lines) + "\n"

    def _query_mapped_reaction_detail_batch(
        self,
        project_id: str,
        mapped_reaction_ids: tuple[str, ...],
    ) -> dict[str, Mapping[str, Any]]:
        if not mapped_reaction_ids:
            return {}
        if len(mapped_reaction_ids) == 1:
            mapped_reaction_id = mapped_reaction_ids[0]
            return {
                mapped_reaction_id: self._query_mapped_reaction_detail(
                    project_id,
                    mapped_reaction_id,
                )
            }
        fields = " ".join(
            f"reaction_{index}: get_mapped_reaction("
            f"project_id: {json.dumps(project_id)} "
            f"mapped_reaction_id: {json.dumps(mapped_reaction_id)}"
            ") { id mapped_reaction_smiles nodes { id role "
            "geometries { geometry_id is_primary mappings { "
            "geometry_atom_map_numbers mapped_smiles verified } } "
            "} edges { transition_state_node_id } }"
            for index, mapped_reaction_id in enumerate(mapped_reaction_ids)
        )
        data = self._transport.compose_query(
            self.config.app_name,
            f"{{ MappedReactionQueryService {{ {fields} }} }}",
        )
        service = data.get("MappedReactionQueryService")
        if not isinstance(service, Mapping):
            raise ReactionDatabaseMcpError(
                "MCP result has no MappedReactionQueryService alias data"
            )
        details: dict[str, Mapping[str, Any]] = {}
        for index, mapped_reaction_id in enumerate(mapped_reaction_ids):
            detail = service.get(f"reaction_{index}")
            if not isinstance(detail, Mapping):
                raise ReactionDatabaseMcpError(
                    f"mapped reaction {mapped_reaction_id} was not returned by the database"
                )
            returned_id = str(detail.get("id", "")).strip()
            if returned_id and returned_id != mapped_reaction_id:
                raise ReactionDatabaseMcpError(
                    f"database returned mapped reaction {returned_id} for "
                    f"requested ID {mapped_reaction_id}"
                )
            details[mapped_reaction_id] = detail
        return details

    def _query_geometry_batch(
        self,
        project_id: str,
        geometry_ids: tuple[str, ...],
    ) -> dict[str, Mapping[str, Any]]:
        if not geometry_ids:
            return {}
        if len(geometry_ids) == 1:
            geometry_id = geometry_ids[0]
            return {geometry_id: self._query_geometry(project_id, geometry_id)}
        fields = " ".join(
            f"geometry_{index}: get_geometry("
            f"project_id: {json.dumps(project_id)} "
            f"geometry_id: {json.dumps(geometry_id)}"
            ") { id coordinates { atom_index element x_angstrom y_angstrom z_angstrom } }"
            for index, geometry_id in enumerate(geometry_ids)
        )
        data = self._transport.compose_query(
            self.config.app_name,
            f"{{ GeometryQueryService {{ {fields} }} }}",
        )
        service = data.get("GeometryQueryService")
        if not isinstance(service, Mapping):
            raise ReactionDatabaseMcpError("MCP result has no GeometryQueryService alias data")
        geometries: dict[str, Mapping[str, Any]] = {}
        for index, geometry_id in enumerate(geometry_ids):
            geometry = service.get(f"geometry_{index}")
            if not isinstance(geometry, Mapping):
                raise ReactionDatabaseMcpError(
                    f"geometry {geometry_id} was not returned by the database"
                )
            returned_id = str(geometry.get("id", "")).strip()
            if returned_id and returned_id != geometry_id:
                raise ReactionDatabaseMcpError(
                    f"database returned geometry {returned_id} for requested ID {geometry_id}"
                )
            geometries[geometry_id] = geometry
        return geometries

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

    @classmethod
    def _thermodynamics_exclusion_reason(
        cls,
        profile: Mapping[str, Any],
    ) -> str | None:
        missing_profiles: list[str] = []
        for field_name in ("reaction", "activation"):
            value = profile.get(field_name)
            if value is None:
                missing_profiles.append(field_name)
            elif not isinstance(value, Mapping):
                raise ReactionDatabaseMcpError(
                    f"thermodynamics profile field {field_name} must be an object"
                )
        if len(missing_profiles) == 2:
            return "missing_reaction_and_activation_profiles"
        if missing_profiles:
            return f"missing_{missing_profiles[0]}_profile"

        missing_energies: list[str] = []
        for profile_name in ("reaction", "activation"):
            energy = profile[profile_name].get("gibbs_free_energy_kcal_mol")
            if energy is None:
                missing_energies.append(profile_name)
            else:
                cls._profile_number(
                    energy,
                    f"{profile_name}.gibbs_free_energy_kcal_mol",
                )
        if len(missing_energies) == 2:
            return "missing_reaction_and_activation_free_energies"
        if missing_energies:
            return f"missing_{missing_energies[0]}_free_energy"
        return None

    @staticmethod
    def _profile_number(value: object, field_name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ReactionDatabaseMcpError(
                f"thermodynamics profile field {field_name} must be numeric"
            )
        converted = float(value)
        if not math.isfinite(converted):
            raise ReactionDatabaseMcpError(
                f"thermodynamics profile field {field_name} must be finite"
            )
        return converted

    @classmethod
    def _optional_profile_number(cls, value: object, field_name: str) -> float | None:
        if value is None:
            return None
        return cls._profile_number(value, field_name)

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
            "nodes { id role geometries { geometry_id is_primary mappings { "
            "geometry_atom_map_numbers mapped_smiles verified } } } "
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

    @staticmethod
    def _thermodynamics_batch_query(
        project_id: str,
        mapped_reaction_ids: tuple[str, ...],
    ) -> str:
        if not 1 <= len(mapped_reaction_ids) <= _RXNRESID_THERMO_ALIAS_BATCH_SIZE:
            raise InvalidRequestError(
                "RxnResid thermodynamics query batches must contain 1 to 24 mapped reactions"
            )
        profile_fields = (
            "mapped_reaction_id profiles { "
            "activation { gibbs_free_energy_kcal_mol } "
            "reaction { gibbs_free_energy_kcal_mol } "
            "reactants_running_time_seconds transition_state_running_time_seconds "
            "products_running_time_seconds }"
        )
        aliases = " ".join(
            "thermodynamics_{}: get_mapped_reaction_thermodynamics(".format(index)
            + f"project_id: {json.dumps(project_id)} "
            + f"mapped_reaction_id: {json.dumps(mapped_reaction_id)}"
            + f") {{ {profile_fields} }}"
            for index, mapped_reaction_id in enumerate(mapped_reaction_ids)
        )
        return "{ ReactionEnergyQueryService { " + aliases + " } }"

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
    def _transition_state_geometry_mapping(
        detail: Mapping[str, Any],
    ) -> tuple[str, tuple[int, ...]]:
        transition_state_node_ids = {
            str(edge.get("transition_state_node_id"))
            for edge in detail.get("edges", [])
            if isinstance(edge, Mapping) and edge.get("transition_state_node_id")
        }
        candidates: list[tuple[int, str, tuple[int, ...]]] = []
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
                mappings = geometry.get("mappings")
                if not isinstance(mappings, list):
                    continue
                for mapping in mappings:
                    if not isinstance(mapping, Mapping) or mapping.get("verified") is not True:
                        continue
                    raw_map_numbers = mapping.get("geometry_atom_map_numbers")
                    if not isinstance(raw_map_numbers, list) or not raw_map_numbers:
                        continue
                    if any(
                        isinstance(number, bool) or not isinstance(number, int) or number < 1
                        for number in raw_map_numbers
                    ):
                        continue
                    map_numbers = tuple(raw_map_numbers)
                    if len(set(map_numbers)) != len(map_numbers):
                        continue
                    score = 8 if node_is_ts else 0
                    if geometry.get("is_primary"):
                        score += 2
                    candidates.append((score, str(geometry["geometry_id"]), map_numbers))
        if not candidates:
            raise ReactionDatabaseMcpError(
                "mapped reaction has no verified transition-state geometry atom mapping"
            )
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        if candidates[0][0] == 0:
            raise ReactionDatabaseMcpError(
                "mapped reaction has geometries but none is identified as a transition state"
            )
        return candidates[0][1], candidates[0][2]

    @staticmethod
    def _geometry_xyz(
        geometry: Mapping[str, Any],
        geometry_id: str,
        *,
        atom_map_numbers: tuple[int, ...] | None = None,
    ) -> str:
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
        if atom_map_numbers is not None:
            if len(atom_map_numbers) != len(rows):
                raise ReactionDatabaseMcpError(
                    f"geometry {geometry_id} atom mapping does not match its coordinate count"
                )
            expected_map_numbers = tuple(range(1, len(rows) + 1))
            if tuple(sorted(atom_map_numbers)) != expected_map_numbers:
                raise ReactionDatabaseMcpError(
                    f"geometry {geometry_id} atom mapping must contain the complete "
                    "1-based reaction atom-map sequence required by RitS"
                )
            rows = [row for _map_number, row in sorted(zip(atom_map_numbers, rows, strict=True))]
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
