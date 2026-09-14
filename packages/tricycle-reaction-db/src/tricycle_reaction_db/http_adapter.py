"""HTTP adapter for the project-scoped reaction database API.

The domain package deliberately keeps the remote API behind
``ReactionDatabaseAdapter``.  This module is the concrete adapter for the
database's documented HTTP API; it does not change the request or receipt
contracts used by the plugin.
"""

from __future__ import annotations

import csv
import io
import json
import os
import ssl
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from tricycle_core import IntegrationNotConfiguredError, InvalidRequestError, load_dotenv
from tricycle_core.errors import TriCycleError

from .contracts import (
    RITS_EXPORT_API_CONTRACT,
    RXNRESID_EXPORT_API_CONTRACT,
    build_rits_mapped_reaction_requests,
    build_rxnresid_csv_export_request,
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

_ENV_KEYS = frozenset(
    {
        "TRICYCLE_REACTION_DB_URL",
        "TRICYCLE_REACTION_DB_TOKEN",
        "TRICYCLE_REACTION_DB_ORGANIZATION_ID",
        "TRICYCLE_REACTION_DB_VERIFY_TLS",
        "TRICYCLE_REACTION_DB_CA_BUNDLE",
        "TRICYCLE_REACTION_DB_TIMEOUT_SECONDS",
        "TRICYCLE_REACTION_DB_UPLOAD_TIMEOUT_SECONDS",
        "TRICYCLE_REACTION_DB_UPLOAD_RETRIES",
        "TRICYCLE_REACTION_DB_RETRY_DELAY_SECONDS",
        "TRICYCLE_REACTION_DB_WAIT_FOR_COMPLETION",
        "TRICYCLE_REACTION_DB_POLL_INTERVAL_SECONDS",
        "TRICYCLE_REACTION_DB_WAIT_TIMEOUT_SECONDS",
    }
)
_IDEMPOTENCY_METADATA_KEY = "tricycle_idempotency_key"
_MANIFEST_SCHEMA_VERSION = "tricycle.calculation-log-manifest/v1"
_UPLOADED_ITEM_STATUSES = frozenset({"staged", "processing", "succeeded"})


class ReactionDatabaseApiError(TriCycleError):
    """A transport or API contract error returned by the database service."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        route: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.route = route
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
class ReactionDatabaseHttpConfig:
    """Runtime settings for :class:`HttpReactionDatabaseAdapter`.

    Credentials are intentionally not part of TOML requests or run state.
    ``from_env`` is called only by the live application composition path.
    """

    base_url: str
    token: str
    organization_id: str | None = None
    verify_tls: bool = True
    ca_bundle: Path | None = None
    timeout_seconds: float = 60.0
    upload_timeout_seconds: float = 3600.0
    upload_retries: int = 3
    retry_delay_seconds: float = 1.0
    wait_for_completion: bool = True
    poll_interval_seconds: float = 5.0
    wait_timeout_seconds: float = 3600.0

    def __post_init__(self) -> None:
        base_url = str(self.base_url).strip().rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise InvalidRequestError("TRICYCLE_REACTION_DB_URL must be an http(s) URL")
        if parsed.query or parsed.fragment:
            raise InvalidRequestError(
                "TRICYCLE_REACTION_DB_URL must not contain a query or fragment"
            )
        token = str(self.token).strip()
        if not token:
            raise IntegrationNotConfiguredError(
                "TRICYCLE_REACTION_DB_TOKEN is required for live database operations"
            )
        organization_id = self.organization_id
        if organization_id is not None:
            organization_id = str(organization_id).strip() or None
        ca_bundle = self.ca_bundle
        if ca_bundle is not None and not isinstance(ca_bundle, Path):
            ca_bundle = Path(str(ca_bundle)).expanduser()
        for name, value in (
            ("timeout_seconds", self.timeout_seconds),
            ("upload_timeout_seconds", self.upload_timeout_seconds),
            ("poll_interval_seconds", self.poll_interval_seconds),
            ("wait_timeout_seconds", self.wait_timeout_seconds),
        ):
            if value <= 0:
                raise InvalidRequestError(f"{name} must be positive")
        if self.upload_retries < 1:
            raise InvalidRequestError("upload_retries must be positive")
        if self.retry_delay_seconds < 0:
            raise InvalidRequestError("retry_delay_seconds must not be negative")
        if self.verify_tls and ca_bundle is not None and not ca_bundle.is_file():
            raise InvalidRequestError(f"database CA bundle does not exist: {ca_bundle}")
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "token", token)
        object.__setattr__(self, "organization_id", organization_id)
        object.__setattr__(self, "ca_bundle", ca_bundle)

    @classmethod
    def from_env(
        cls,
        env_file: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> ReactionDatabaseHttpConfig:
        """Build live adapter settings from process environment and ``.env``."""

        values = dict(os.environ if environ is None else environ)
        if environ is None or env_file is not None:
            load_dotenv(env_file, environ=values, keys=_ENV_KEYS)
        base_url = values.get("TRICYCLE_REACTION_DB_URL", "").strip()
        token = values.get("TRICYCLE_REACTION_DB_TOKEN", "").strip()
        if not base_url:
            raise IntegrationNotConfiguredError(
                "TRICYCLE_REACTION_DB_URL is required for live database operations"
            )
        if not token:
            raise IntegrationNotConfiguredError(
                "TRICYCLE_REACTION_DB_TOKEN is required for live database operations"
            )
        ca_bundle_value = values.get("TRICYCLE_REACTION_DB_CA_BUNDLE", "").strip()
        return cls(
            base_url=base_url,
            token=token,
            organization_id=values.get("TRICYCLE_REACTION_DB_ORGANIZATION_ID"),
            verify_tls=_env_bool(values, "TRICYCLE_REACTION_DB_VERIFY_TLS", True),
            ca_bundle=Path(ca_bundle_value).expanduser() if ca_bundle_value else None,
            timeout_seconds=_env_float(values, "TRICYCLE_REACTION_DB_TIMEOUT_SECONDS", 60.0),
            upload_timeout_seconds=_env_float(
                values,
                "TRICYCLE_REACTION_DB_UPLOAD_TIMEOUT_SECONDS",
                3600.0,
            ),
            upload_retries=_env_int(values, "TRICYCLE_REACTION_DB_UPLOAD_RETRIES", 3),
            retry_delay_seconds=_env_float(
                values,
                "TRICYCLE_REACTION_DB_RETRY_DELAY_SECONDS",
                1.0,
            ),
            wait_for_completion=_env_bool(
                values,
                "TRICYCLE_REACTION_DB_WAIT_FOR_COMPLETION",
                True,
            ),
            poll_interval_seconds=_env_float(
                values,
                "TRICYCLE_REACTION_DB_POLL_INTERVAL_SECONDS",
                5.0,
            ),
            wait_timeout_seconds=_env_float(
                values,
                "TRICYCLE_REACTION_DB_WAIT_TIMEOUT_SECONDS",
                3600.0,
            ),
        )


class _MultipartFileBody:
    """A bounded, streaming multipart body for one calculation log."""

    def __init__(self, path: Path, *, boundary: str, chunk_size: int = 1024 * 1024) -> None:
        filename = (
            path.name.replace('"', "_").replace("\\", "_").replace("\r", "_").replace("\n", "_")
        )
        self._prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: text/plain\r\n\r\n"
        ).encode()
        self._suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        self._path = path
        self._chunk_size = chunk_size
        self._pending = self._prefix
        self._file: BinaryIO | None = None
        self._file_finished = False
        self._done = False
        self.content_length = len(self._prefix) + path.stat().st_size + len(self._suffix)

    def read(self, size: int = -1) -> bytes:
        if self._done:
            return b""
        if size < 0:
            size = self._chunk_size
        if size == 0:
            return b""
        output = bytearray()
        while len(output) < size and not self._done:
            if self._pending:
                take = min(size - len(output), len(self._pending))
                output.extend(self._pending[:take])
                self._pending = self._pending[take:]
                continue
            if not self._file_finished:
                if self._file is None:
                    self._file = self._path.open("rb")
                chunk = self._file.read(min(self._chunk_size, size - len(output)))
                if chunk:
                    output.extend(chunk)
                    continue
                self._file.close()
                self._file = None
                self._file_finished = True
                self._pending = self._suffix
                continue
            self._done = True
        return bytes(output)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        self._done = True


class HttpReactionDatabaseAdapter(ReactionDatabaseAdapter):
    """Concrete adapter for the database's documented HTTP API.

    Batch imports create (or safely reuse) one project, register one upload
    batch with deterministic client file IDs, upload only the resolved log
    manifest, and optionally wait for the server-side parser.  Retrying the
    same request is safe because project and upload-batch records carry the
    deterministic import key and individual files use deterministic UUIDs.
    """

    def __init__(self, config: ReactionDatabaseHttpConfig) -> None:
        self.config = config

    @classmethod
    def from_env(
        cls,
        env_file: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> HttpReactionDatabaseAdapter:
        return cls(ReactionDatabaseHttpConfig.from_env(env_file, environ=environ))

    def ingest_calculation_logs(
        self,
        request: ReactionBatchImportRequest,
        manifest: CalculationLogBatchManifest,
    ) -> ReactionDatabaseImportReceipt:
        idempotency_key = request.resolved_idempotency_key(manifest)
        project_id = self._create_or_reuse_project(request, manifest, idempotency_key)
        file_specs = self._file_specs(manifest, idempotency_key)
        upload_batch = self._create_or_reuse_upload_batch(
            request,
            manifest,
            project_id,
            idempotency_key,
            file_specs,
        )
        upload_batch_id = self._required_id(upload_batch, "upload batch", "id")
        if str(upload_batch.get("status", "")).casefold() == "cancelled":
            raise ReactionDatabaseApiError(
                f"upload batch {upload_batch_id} is cancelled; start a new idempotency key",
                route=f"/api/upload-batches/{upload_batch_id}",
            )
        if str(upload_batch.get("status", "")).casefold() == "paused":
            upload_batch = self._request_json(
                "PATCH",
                f"/api/upload-batches/{upload_batch_id}",
                json_body={"status": "active"},
            )

        existing_items = self._list_upload_items(project_id, upload_batch_id)
        uploaded_count = 0
        skipped_count = 0
        errors: list[str] = []
        for client_file_id, path, _relative_path in file_specs:
            existing = existing_items.get(client_file_id)
            existing_status = str((existing or {}).get("status", "")).casefold()
            if existing_status in _UPLOADED_ITEM_STATUSES:
                skipped_count += 1
                continue
            try:
                item = self._upload_file_with_retry(upload_batch_id, client_file_id, path)
                item_status = str(item.get("status", "")).casefold()
                if item_status == "failed":
                    errors.append(
                        f"{client_file_id}: {item.get('error_message') or item.get('error_code') or 'server rejected file'}"
                    )
                else:
                    uploaded_count += 1
            except ReactionDatabaseApiError as exc:
                errors.append(f"{client_file_id}: {exc}")

        final_batch = self._get_upload_batch(project_id, upload_batch_id)
        if not errors and self.config.wait_for_completion:
            final_batch = self._wait_for_batch(project_id, upload_batch_id, final_batch)

        server_failed_count = self._nonnegative_int(final_batch.get("failed_count"))
        cancelled_count = self._nonnegative_int(final_batch.get("cancelled_count"))
        failed_count = max(server_failed_count, cancelled_count, len(errors))
        total_count = len(file_specs)
        accepted_count = max(0, total_count - failed_count)
        batch_status = str(final_batch.get("status", "")).casefold()
        if failed_count:
            status = "failed"
        elif batch_status == "completed":
            status = "succeeded"
        elif batch_status == "cancelled":
            status = "failed"
        else:
            status = "submitted"
        metadata: dict[str, object] = {
            "project_id": project_id,
            "upload_batch_id": upload_batch_id,
            "batch_status": batch_status or None,
            "manifest_digest": manifest.manifest_digest,
            "total_count": total_count,
            "uploaded_count": uploaded_count,
            "skipped_count": skipped_count,
            "wait_for_completion": self.config.wait_for_completion,
            "server_counts": {
                key: self._nonnegative_int(final_batch.get(key))
                for key in (
                    "succeeded_count",
                    "failed_count",
                    "cancelled_count",
                    "uploading_count",
                    "staged_count",
                    "processing_count",
                )
                if key in final_batch
            },
        }
        if errors:
            metadata["errors"] = errors[:20]
            metadata["error_count"] = len(errors)
        return ReactionDatabaseImportReceipt(
            status=status,
            project_id=project_id,
            accepted_log_count=accepted_count,
            failed_log_count=failed_count,
            metadata=metadata,
        )

    def filter_cleaned_reactions(self, request: ReactionFilterRequest) -> ReactionFilterReceipt:
        payload = self._request_json(
            "GET",
            "/api/mapped-reactions",
            query={
                "project_id": request.project_id,
                "reaction_smarts": request.reaction_smarts,
                "limit": 1,
                "offset": 0,
            },
        )
        page = payload.get("page", {}) if isinstance(payload, Mapping) else {}
        raw_total = page.get("total", -1) if isinstance(page, Mapping) else -1
        try:
            total = int(raw_total)
        except (TypeError, ValueError):
            total = -1
        selected_count = total if total >= 0 else self._count_mapped_reactions(request)
        return ReactionFilterReceipt(
            project_id=request.project_id,
            selected_count=selected_count,
            reaction_smarts=request.reaction_smarts,
            metadata={
                "api_route": "/api/mapped-reactions",
                "reaction_smarts": request.reaction_smarts,
                "cleaned_only": request.cleaned_only,
            },
        )

    def export_rits_dataset(self, request: RitsDatasetExportRequest) -> ReactionSpaceExportReceipt:
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
                for summary in self._iter_mapped_reactions(
                    request.project_id, request.reaction_smarts
                ):
                    mapped_reaction_id = self._required_id(summary, "mapped reaction", "id")
                    detail = self._request_json(
                        "GET",
                        f"/api/mapped-reactions/{mapped_reaction_id}",
                        query={"project_id": request.project_id},
                    )
                    geometry_id = self._transition_state_geometry_id(detail)
                    xyz, _headers = self._request(
                        "GET",
                        f"/api/depictions/geometry/{geometry_id}.xyz",
                        query={"project_id": request.project_id},
                    )
                    writer.writerow(
                        (
                            str(
                                detail.get(
                                    "mapped_reaction_smiles",
                                    summary.get("mapped_reaction_smiles", ""),
                                )
                            ),
                            xyz.decode("utf-8"),
                        )
                    )
                    row_count += 1
            assert temporary_path is not None
            os.replace(temporary_path, destination)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        return ReactionSpaceExportReceipt(
            destination=destination,
            row_count=row_count,
            api_contract=RITS_EXPORT_API_CONTRACT,
            api_requests=build_rits_mapped_reaction_requests(
                request.project_id,
                request.reaction_smarts,
            ),
            metadata={"api_route": "/api/mapped-reactions", "structure_format": "xyz"},
        )

    def export_rxnresid_dataset(
        self,
        request: RxnResidDatasetExportRequest,
    ) -> ReactionSpaceExportReceipt:
        api_request = build_rxnresid_csv_export_request(
            request.project_id,
            request.reaction_smarts,
        )
        data, headers = self._request(
            api_request.method,
            api_request.route,
            json_body=api_request.body,
        )
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(request.destination, data)
        return ReactionSpaceExportReceipt(
            destination=request.destination,
            row_count=self._csv_row_count(data),
            api_contract=RXNRESID_EXPORT_API_CONTRACT,
            api_requests=(api_request,),
            metadata={
                "api_route": api_request.route,
                "content_type": headers.get("content-type", "text/csv"),
                "energy_unit": request.energy_unit,
                "timing_unit": request.timing_unit,
            },
        )

    def _create_or_reuse_project(
        self,
        request: ReactionBatchImportRequest,
        manifest: CalculationLogBatchManifest,
        idempotency_key: str,
    ) -> str:
        organization_id = request.project.organization_id or self.config.organization_id
        if not organization_id:
            raise IntegrationNotConfiguredError(
                "reaction database imports require project.organization_id or "
                "TRICYCLE_REACTION_DB_ORGANIZATION_ID"
            )
        projects = self._request_json(
            "GET",
            "/api/projects",
            query={"include_archived": "true"},
        )
        if not isinstance(projects, list):
            raise ReactionDatabaseApiError("database project list response must be an array")
        for project in projects:
            if not isinstance(project, Mapping) or project.get("slug") != request.project.slug:
                continue
            data_source = project.get("data_source")
            if (
                isinstance(data_source, Mapping)
                and data_source.get(_IDEMPOTENCY_METADATA_KEY) == idempotency_key
            ):
                if data_source.get("manifest_digest") not in {None, manifest.manifest_digest}:
                    raise ReactionDatabaseApiError(
                        "existing project has a different manifest for the same import key",
                        route="/api/projects",
                    )
                return self._required_id(project, "project", "id")
            raise ReactionDatabaseApiError(
                f"project slug already exists with different import provenance: {request.project.slug}",
                route="/api/projects",
            )

        data_source = {
            "adapter": "tricycle-reaction-db.http",
            _IDEMPOTENCY_METADATA_KEY: idempotency_key,
            "source": request.source,
            "batch_id": manifest.batch_id,
            "manifest_digest": manifest.manifest_digest,
            "batch_metadata": dict(manifest.metadata),
            "request_metadata": dict(request.metadata),
        }
        body = {
            "organization_id": organization_id,
            "slug": request.project.slug,
            "name": request.project.name,
            "data_source": data_source,
            "calculation_protocol": dict(request.provenance),
        }
        project = self._request_json(
            "POST",
            "/api/projects",
            json_body=body,
            idempotency_key=idempotency_key,
        )
        if not isinstance(project, Mapping):
            raise ReactionDatabaseApiError("database project creation response must be an object")
        return self._required_id(project, "project", "id")

    def _create_or_reuse_upload_batch(
        self,
        request: ReactionBatchImportRequest,
        manifest: CalculationLogBatchManifest,
        project_id: str,
        idempotency_key: str,
        file_specs: list[tuple[str, Path, str]],
    ) -> Mapping[str, Any]:
        existing = self._find_upload_batch(project_id, idempotency_key, manifest.manifest_digest)
        if existing is not None:
            if self._nonnegative_int(existing.get("total_count")) != len(file_specs):
                raise ReactionDatabaseApiError(
                    "existing upload batch has a different file count for the same import key",
                    route="/api/upload-batches",
                )
            return existing
        body = {
            "project_id": project_id,
            "artifact_kind": "calculation_output",
            "shared_metadata": {
                "adapter": "tricycle-reaction-db.http",
                _IDEMPOTENCY_METADATA_KEY: idempotency_key,
                "source": request.source,
                "batch_id": manifest.batch_id,
                "manifest_digest": manifest.manifest_digest,
                "provenance": dict(request.provenance),
                "batch_metadata": dict(manifest.metadata),
                "request_metadata": dict(request.metadata),
            },
            "manifest_sha256": manifest.manifest_digest,
            "manifest_schema_version": _MANIFEST_SCHEMA_VERSION,
            "files": [
                {
                    "client_file_id": client_file_id,
                    "original_filename": path.name,
                    "relative_path": relative_path,
                    "size_bytes": path.stat().st_size,
                    "media_type": "text/plain",
                    "is_gaussian_log": True,
                    "selection_status": "selected",
                }
                for client_file_id, path, relative_path in file_specs
            ],
        }
        result = self._request_json(
            "POST",
            "/api/upload-batches",
            json_body=body,
            idempotency_key=idempotency_key,
        )
        if not isinstance(result, Mapping):
            raise ReactionDatabaseApiError("upload batch creation response must be an object")
        return result

    def _find_upload_batch(
        self,
        project_id: str,
        idempotency_key: str,
        manifest_digest: str,
    ) -> Mapping[str, Any] | None:
        offset = 0
        while True:
            payload = self._request_json(
                "GET",
                "/api/upload-batches",
                query={"project_id": project_id, "limit": 100, "offset": offset},
            )
            if not isinstance(payload, Mapping):
                raise ReactionDatabaseApiError("upload batch list response must be an object")
            items = payload.get("items", [])
            if not isinstance(items, list):
                raise ReactionDatabaseApiError("upload batch list items must be an array")
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                shared = item.get("shared_metadata", {})
                if (
                    not isinstance(shared, Mapping)
                    or shared.get(_IDEMPOTENCY_METADATA_KEY) != idempotency_key
                ):
                    continue
                remote_digest = item.get("manifest_sha256") or shared.get("manifest_digest")
                if remote_digest and remote_digest != manifest_digest:
                    raise ReactionDatabaseApiError(
                        "existing upload batch has a different manifest for the same import key",
                        route="/api/upload-batches",
                    )
                return item
            total = self._nonnegative_int(payload.get("total"))
            offset += len(items)
            if not items or (total and offset >= total) or len(items) < 100:
                return None

    def _get_upload_batch(self, project_id: str, upload_batch_id: str) -> Mapping[str, Any]:
        result = self._request_json(
            "GET",
            f"/api/upload-batches/{upload_batch_id}",
            query={"project_id": project_id},
        )
        if not isinstance(result, Mapping):
            raise ReactionDatabaseApiError("upload batch response must be an object")
        return result

    def _list_upload_items(
        self, project_id: str, upload_batch_id: str
    ) -> dict[str, Mapping[str, Any]]:
        offset = 0
        result: dict[str, Mapping[str, Any]] = {}
        while True:
            payload = self._request_json(
                "GET",
                f"/api/upload-batches/{upload_batch_id}/items",
                query={
                    "project_id": project_id,
                    "limit": 200,
                    "offset": offset,
                },
            )
            if not isinstance(payload, Mapping):
                raise ReactionDatabaseApiError("upload batch item response must be an object")
            items = payload.get("items", [])
            if not isinstance(items, list):
                raise ReactionDatabaseApiError("upload batch item list must be an array")
            for item in items:
                if isinstance(item, Mapping) and item.get("client_file_id"):
                    result[str(item["client_file_id"])] = item
            total = self._nonnegative_int(payload.get("total"))
            offset += len(items)
            if not items or (total and offset >= total) or len(items) < 200:
                return result

    def _upload_file_with_retry(
        self,
        upload_batch_id: str,
        client_file_id: str,
        path: Path,
    ) -> Mapping[str, Any]:
        last_error: ReactionDatabaseApiError | None = None
        for attempt in range(self.config.upload_retries):
            try:
                result = self._request_json(
                    "POST",
                    f"/api/upload-batches/{upload_batch_id}/files/{client_file_id}",
                    multipart_path=path,
                    timeout=self.config.upload_timeout_seconds,
                )
                if not isinstance(result, Mapping):
                    raise ReactionDatabaseApiError("uploaded item response must be an object")
                return result
            except ReactionDatabaseApiError as exc:
                last_error = exc
                status = exc.status_code
                retryable = status is None or status in {408, 425, 429} or status >= 500
                if not retryable or attempt + 1 >= self.config.upload_retries:
                    raise
                if self.config.retry_delay_seconds:
                    time.sleep(self.config.retry_delay_seconds * (2**attempt))
        assert last_error is not None
        raise last_error

    def _wait_for_batch(
        self,
        project_id: str,
        upload_batch_id: str,
        initial: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        current = initial
        deadline = time.monotonic() + self.config.wait_timeout_seconds
        while True:
            status = str(current.get("status", "")).casefold()
            if status == "completed":
                return current
            if status == "cancelled":
                return current
            if time.monotonic() >= deadline:
                raise ReactionDatabaseApiError(
                    f"timed out waiting for upload batch {upload_batch_id}; resume to poll again",
                    route=f"/api/upload-batches/{upload_batch_id}",
                )
            time.sleep(self.config.poll_interval_seconds)
            current = self._get_upload_batch(project_id, upload_batch_id)

    def _file_specs(
        self,
        manifest: CalculationLogBatchManifest,
        idempotency_key: str,
    ) -> list[tuple[str, Path, str]]:
        namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"tricycle-reaction-db:{idempotency_key}")
        result: list[tuple[str, Path, str]] = []
        for path in manifest.files:
            relative_path = path.relative_to(manifest.root).as_posix()
            client_file_id = str(uuid.uuid5(namespace, relative_path))
            result.append((client_file_id, path, relative_path))
        return result

    def _iter_mapped_reactions(
        self,
        project_id: str,
        reaction_smarts: str,
    ) -> Iterator[Mapping[str, Any]]:
        offset = 0
        while True:
            payload = self._request_json(
                "GET",
                "/api/mapped-reactions",
                query={
                    "project_id": project_id,
                    "reaction_smarts": reaction_smarts,
                    "limit": 200,
                    "offset": offset,
                },
            )
            if not isinstance(payload, Mapping):
                raise ReactionDatabaseApiError("mapped reaction response must be an object")
            items = payload.get("items", [])
            if not isinstance(items, list):
                raise ReactionDatabaseApiError("mapped reaction items must be an array")
            yield from (item for item in items if isinstance(item, Mapping))
            offset += len(items)
            page = payload.get("page", {})
            total = self._nonnegative_int(page.get("total")) if isinstance(page, Mapping) else 0
            if not items or (total and offset >= total) or len(items) < 200:
                return

    def _count_mapped_reactions(self, request: ReactionFilterRequest) -> int:
        return sum(
            1 for _ in self._iter_mapped_reactions(request.project_id, request.reaction_smarts)
        )

    def _transition_state_geometry_id(self, detail: Mapping[str, Any]) -> str:
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
                score = 0
                energy_view = geometry.get("energy_view", {})
                if node_is_ts:
                    score += 8
                if geometry.get("is_primary"):
                    score += 2
                if isinstance(energy_view, Mapping) and energy_view.get("is_transition_state"):
                    score += 16
                candidates.append((score, str(geometry["geometry_id"])))
        if not candidates:
            raise ReactionDatabaseApiError(
                "mapped reaction has no transition-state geometry",
                route="/api/mapped-reactions/{mapped_reaction_id}",
            )
        candidates.sort(reverse=True)
        if candidates[0][0] == 0:
            raise ReactionDatabaseApiError(
                "mapped reaction has geometries but none is identified as a transition state",
                route="/api/mapped-reactions/{mapped_reaction_id}",
            )
        return candidates[0][1]

    def _request_json(
        self,
        method: str,
        route: str,
        *,
        query: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        multipart_path: Path | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        data, _headers = self._request(
            method,
            route,
            query=query,
            json_body=json_body,
            multipart_path=multipart_path,
            idempotency_key=idempotency_key,
            timeout=timeout,
        )
        if not data:
            return {}
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReactionDatabaseApiError(
                f"database API returned invalid JSON for {route}",
                route=route,
            ) from exc

    def _request(
        self,
        method: str,
        route: str,
        *,
        query: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        multipart_path: Path | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, Mapping[str, str]]:
        if json_body is not None and multipart_path is not None:
            raise InvalidRequestError("database request cannot contain JSON and multipart bodies")
        url = self._url(route, query)
        headers = {
            "Accept": "application/json, text/csv, text/plain",
            "Authorization": f"Bearer {self.config.token}",
            "User-Agent": "TriCycle-reaction-db/0.1",
        }
        body: bytes | _MultipartFileBody | None = None
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        elif multipart_path is not None:
            boundary = f"tricycle-{uuid.uuid4().hex}"
            body = _MultipartFileBody(multipart_path, boundary=boundary)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            headers["Content-Length"] = str(body.content_length)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(url, data=body, headers=headers, method=method.upper())
        open_kwargs: dict[str, Any] = {"timeout": timeout or self.config.timeout_seconds}
        if urlsplit(url).scheme == "https":
            open_kwargs["context"] = self._ssl_context()
        try:
            with urlopen(request, **open_kwargs) as response:
                raw = response.read()
                response_headers = {
                    str(key).casefold(): str(value) for key, value in response.headers.items()
                }
                status_code = int(response.status)
        except HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace").strip()
            suffix = f": {detail[:500]}" if detail else ""
            raise ReactionDatabaseApiError(
                f"database API HTTP {exc.code} for {route}{suffix}",
                status_code=exc.code,
                route=route,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ReactionDatabaseApiError(
                f"database API request failed for {route}: {exc}",
                route=route,
            ) from exc
        finally:
            if isinstance(body, _MultipartFileBody):
                body.close()
        if not 200 <= status_code < 300:
            raise ReactionDatabaseApiError(
                f"database API HTTP {status_code} for {route}",
                status_code=status_code,
                route=route,
            )
        return raw, response_headers

    def _ssl_context(self) -> ssl.SSLContext:
        if not self.config.verify_tls:
            return ssl._create_unverified_context()
        if self.config.ca_bundle is not None:
            return ssl.create_default_context(cafile=str(self.config.ca_bundle))
        return ssl.create_default_context()

    def _url(self, route: str, query: Mapping[str, object] | None) -> str:
        if not route.startswith("/"):
            raise InvalidRequestError("database API route must start with '/'")
        url = f"{self.config.base_url}{route}"
        values = [(key, str(value)) for key, value in (query or {}).items() if value is not None]
        if values:
            url = f"{url}?{urlencode(values, doseq=True)}"
        return url

    @staticmethod
    def _required_id(payload: Mapping[str, Any], resource: str, key: str) -> str:
        value = str(payload.get(key, "")).strip()
        if not value:
            raise ReactionDatabaseApiError(f"database {resource} response has no {key}")
        return value

    @staticmethod
    def _nonnegative_int(value: object) -> int:
        if isinstance(value, bool):
            return int(value)
        if not isinstance(value, (int, float, str)):
            return 0
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            return 0
        return max(0, parsed)

    @staticmethod
    def _atomic_write(destination: Path, data: bytes) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(data)
            assert temporary_path is not None
            os.replace(temporary_path, destination)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _csv_row_count(data: bytes) -> int:
        text = data.decode("utf-8")
        rows = list(csv.reader(io.StringIO(text)))
        return max(0, len(rows) - 1)


def create_reaction_database_adapter(
    env_file: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> HttpReactionDatabaseAdapter:
    """Compose the production HTTP adapter for the live application path."""

    return HttpReactionDatabaseAdapter.from_env(env_file, environ=environ)


__all__ = [
    "HttpReactionDatabaseAdapter",
    "ReactionDatabaseApiError",
    "ReactionDatabaseHttpConfig",
    "create_reaction_database_adapter",
]
