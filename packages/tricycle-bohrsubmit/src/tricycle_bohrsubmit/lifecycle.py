from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shlex
import tempfile
import time
import zipfile
import zlib
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tricycle_core import Artifact, BohrJobSpec
from tricycle_core.errors import InvalidRequestError

from .config import flatten_sections, load_toml
from .gateway import BohrWorkflowGateway

SUBMIT_FIELDS = ("worker", "tasks", "job_id", "job_name", "status", "input_dir")
STATUS_FIELDS = (
    "worker",
    "tasks",
    "job_id",
    "job_name",
    "submit_status",
    "job_status",
    "terminal",
    "result",
    "input_dir",
    "checked_at",
)
DOWNLOAD_FIELDS = ("worker", "tasks", "job_id", "job_name", "status", "download_dir")
_BOHR_ID_MAP = "bohr-id-map.tsv"
_DOWNLOAD_MANIFEST = "download-manifest.txt"
_DOWNLOAD_MARKER = ".bohr-download-unzipped"
_DOWNLOAD_STATE_VERSION = 1
_INTERNAL_ID_FIELDS = ("job_id", "internal_job_id", "internal_id", "id")
_PUBLIC_ID_FIELDS = (
    "bohr_id",
    "bohrId",
    "bohr_job_id",
    "bohrJobId",
    "public_job_id",
)


@dataclass(frozen=True, slots=True)
class LifecycleSummary:
    total: int
    succeeded: int = 0
    failed: int = 0
    incomplete: int = 0
    skipped: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "incomplete": self.incomplete,
            "skipped": self.skipped,
        }


def _write_tsv(path: Path, fields: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)


def _write_tsv_atomic(path: Path, fields: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows({field: row.get(field, "") for field in fields} for row in rows)
    _atomic_write_text(path, buffer.getvalue())


def _read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise InvalidRequestError(f"TSV file not found: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter="\t")]


def _read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise InvalidRequestError(f"environment file not found: {path}")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        try:
            parsed = shlex.split(raw, comments=False, posix=True)
        except ValueError as exc:
            raise InvalidRequestError(f"invalid environment line in {path}: {line}") from exc
        values[key.strip()] = parsed[0] if parsed else ""
    return values


def read_run_config(run_dir: Path) -> dict[str, object]:
    """Read declarative run metadata, retaining legacy env compatibility."""

    toml_path = run_dir / "batch.toml"
    if toml_path.is_file():
        return flatten_sections(load_toml(toml_path))
    return _read_env(run_dir / "batch.env")


def _job_group_id_for_run(run_dir: Path) -> str | None:
    """Return the group id used by the Bohr CLI for this prepared run."""

    if not ((run_dir / "batch.toml").is_file() or (run_dir / "batch.env").is_file()):
        return None
    config = read_run_config(run_dir)
    value = config.get("job_group_id")
    group_id = str(value or "").strip()
    return group_id or None


def _read_job_id_map(run_dir: Path) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    path = run_dir / _BOHR_ID_MAP
    if not path.is_file():
        return (), []
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            fields = tuple(reader.fieldnames or ())
            if not fields:
                raise InvalidRequestError(f"Bohr id map has no header: {path}")
            return fields, [dict(row) for row in reader]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise InvalidRequestError(f"cannot read Bohr id map: {path}") from exc


def _first_row_value(row: dict[str, str], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = str(row.get(field, "") or "").strip()
        if value:
            return value
    return ""


def _read_job_id_aliases(run_dir: Path) -> dict[str, str]:
    _, rows = _read_job_id_map(run_dir)
    aliases: dict[str, str] = {}
    for row in rows:
        internal_id = _first_row_value(row, _INTERNAL_ID_FIELDS)
        public_id = _first_row_value(row, _PUBLIC_ID_FIELDS)
        if internal_id and public_id:
            aliases[internal_id] = public_id
    return aliases


def _persist_job_id_alias(run_dir: Path, internal_id: str, public_id: str) -> None:
    path = run_dir / _BOHR_ID_MAP
    fields, rows = _read_job_id_map(run_dir)
    mutable_fields = list(fields or ("internal_job_id", "bohr_id"))
    internal_field = next(
        (field for field in mutable_fields if field in _INTERNAL_ID_FIELDS),
        "internal_job_id",
    )
    public_field = next(
        (field for field in mutable_fields if field in _PUBLIC_ID_FIELDS),
        "bohr_id",
    )
    for field in (internal_field, public_field):
        if field not in mutable_fields:
            mutable_fields.append(field)
    for row in rows:
        if _first_row_value(row, (internal_field, *_INTERNAL_ID_FIELDS)) == internal_id:
            row[public_field] = public_id
            break
    else:
        rows.append({internal_field: internal_id, public_field: public_id})
    _write_tsv_atomic(path, tuple(mutable_fields), rows)


def _resolve_job_id(
    run_dir: Path,
    gateway: BohrWorkflowGateway,
    job_id: str,
    *,
    job_group_id: str | None,
    aliases: dict[str, str] | None = None,
) -> str:
    """Resolve legacy platform ids when the injected adapter supports it."""

    identifier = str(job_id).strip()
    if not identifier:
        raise InvalidRequestError("job id must not be empty")
    known_aliases = aliases if aliases is not None else _read_job_id_aliases(run_dir)
    known = known_aliases.get(identifier)
    if known:
        return known

    resolver = getattr(gateway, "resolve_job_id", None)
    resolved = resolver(identifier, job_group_id=job_group_id) if callable(resolver) else identifier
    resolved_id = str(resolved).strip()
    if not resolved_id:
        raise InvalidRequestError(f"Bohr adapter resolved an empty job id for {identifier}")
    if resolved_id != identifier:
        _persist_job_id_alias(run_dir, identifier, resolved_id)
    known_aliases[identifier] = resolved_id
    return resolved_id


def read_submit_rows(run_dir: Path) -> list[dict[str, str]]:
    return _read_tsv(run_dir / "submit-results.tsv")


def _run_list(run_dir: Path, config: dict[str, object], key: str, legacy_name: str) -> list[str]:
    value = config.get(key)
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value:
        return [value]
    legacy_path = run_dir / legacy_name
    if legacy_path.is_file():
        return [line for line in legacy_path.read_text(encoding="utf-8").splitlines() if line]
    return []


def job_for_row(run_dir: Path, row: dict[str, str], *, job_group_id: str | None = None) -> BohrJobSpec:
    config = read_run_config(run_dir)
    worker = row.get("worker", "")
    input_dir = row.get("input_dir", "")
    if not input_dir:
        raise InvalidRequestError(f"submit row has no input_dir: {worker}")
    is_dpa = (run_dir / worker / "work.sh").is_file() and (run_dir / worker / "dpa-berny.env").is_file()
    log_file = str(config.get("log_file", "worker.log"))
    command = f"bash {'work.sh' if is_dpa else 'run_worker.sh'} > {log_file} 2>&1"
    dataset_paths = _run_list(run_dir, config, "dataset_paths", "dataset-paths.txt")
    extra_submit_args = _run_list(
        run_dir, config, "extra_submit_args", "submit-extra-args.txt"
    )
    parameters: dict[str, object] = {
        "input_directory": input_dir,
        "command_string": command,
        "log_file": log_file,
        "backward_files": config.get(
            "backward_files",
            "worker.log,server.log,*.log,*.out,*.xyz,*.chk,results"
            if is_dpa
            else "worker.log,results,manifest.txt,manifest.tsv",
        ),
        "job_group_id": job_group_id or str(config.get("job_group_id", "")),
        "result_path": config.get("result_path", ""),
        "max_reschedule_times": config.get("max_reschedule_times", ""),
        "max_run_time": config.get("max_run_time", ""),
        "nnode": config.get("nnode", ""),
        "dataset_paths": dataset_paths,
        "extra_submit_args": extra_submit_args,
    }
    input_artifact = Artifact(
        artifact_id=f"{run_dir.name}-{worker}-input",
        location=input_dir,
        kind="directory",
    )
    return BohrJobSpec(
        name=row.get("job_name", "") or f"{config.get('job_prefix', run_dir.name)}-{worker}",
        operation="bohr.batch.worker",
        image=str(config.get("image", "")),
        command=("bash", "work.sh" if is_dpa else "run_worker.sh"),
        project_id=str(config.get("project_id")) if config.get("project_id") else None,
        machine_type=str(config.get("machine_type")) if config.get("machine_type") else None,
        input_artifacts=(input_artifact,),
        parameters=parameters,
        tags=("tricycle", "bohr-submit", "batch"),
    )


def normalize_status(value: object) -> str:
    return "".join(str(value or "").lower().replace("_", "").replace("-", "").split())


def classify_status(value: object) -> tuple[bool, str]:
    normalized = normalize_status(value)
    if normalized in {"finish", "finished", "success", "succeeded", "complete", "completed", "done"}:
        return True, "SUCCEEDED"
    if normalized in {
        "fail",
        "failed",
        "error",
        "stopped",
        "terminated",
        "killed",
        "cancel",
        "canceled",
        "cancelled",
    }:
        return True, "FAILED"
    if normalized in {
        "pending",
        "wait",
        "waiting",
        "schedule",
        "scheduling",
        "queued",
        "queue",
        "submitted",
        "created",
    }:
        return False, "PENDING"
    if normalized in {"run", "running", "executing", "execute"}:
        return False, "RUNNING"
    return False, "UNKNOWN"


def status_run(run_dir: Path, gateway: BohrWorkflowGateway) -> tuple[Path, LifecycleSummary]:
    rows: list[dict[str, object]] = []
    query_failed = 0
    succeeded = failed = incomplete = skipped = 0
    checked_at = datetime.now(UTC).isoformat()
    job_group_id = _job_group_id_for_run(run_dir)
    job_id_aliases = _read_job_id_aliases(run_dir)
    for source in read_submit_rows(run_dir):
        row: dict[str, object] = {
            "worker": source.get("worker", ""),
            "tasks": source.get("tasks", ""),
            "job_id": source.get("job_id", ""),
            "job_name": source.get("job_name", ""),
            "submit_status": source.get("status", ""),
            "job_status": "",
            "terminal": "no",
            "result": "UNKNOWN",
            "input_dir": source.get("input_dir", ""),
            "checked_at": checked_at,
        }
        job_id = str(source.get("job_id", ""))
        if not job_id:
            submit_status = source.get("status", "")
            if submit_status in {"SKIPPED_EMPTY", "DRY_RUN", "PREPARED"}:
                row["terminal"] = "yes"
                row["result"] = submit_status
                skipped += 1
            elif submit_status == "FAILED":
                row["terminal"] = "yes"
                row["result"] = "SUBMIT_FAILED"
                failed += 1
            else:
                incomplete += 1
        else:
            try:
                resolved_job_id = _resolve_job_id(
                    run_dir,
                    gateway,
                    job_id,
                    job_group_id=job_group_id,
                    aliases=job_id_aliases,
                )
                remote = gateway.status(resolved_job_id)
                raw_status = remote.get("status", "")
                terminal, result = classify_status(raw_status)
                row["job_status"] = raw_status
                row["terminal"] = "yes" if terminal else "no"
                row["result"] = result
                if result == "SUCCEEDED":
                    succeeded += 1
                elif result == "FAILED":
                    failed += 1
                else:
                    incomplete += 1
            except Exception:
                query_failed += 1
                row["job_status"] = "QUERY_FAILED"
                row["result"] = "QUERY_FAILED"
                incomplete += 1
        rows.append(row)

    output = run_dir / "status.tsv"
    _write_tsv(output, STATUS_FIELDS, rows)
    if query_failed:
        raise InvalidRequestError(f"{query_failed} Bohr status query(s) failed; see {output}")
    return output, LifecycleSummary(len(rows), succeeded, failed, incomplete, skipped)


def _safe_member_target(destination: Path, member_name: str) -> Path:
    root = destination.resolve()
    target = (destination / member_name).resolve()
    if target != root and root not in target.parents:
        raise InvalidRequestError(f"unsafe archive member: {member_name}")
    return target


def _archive_infos(zip_path: Path, destination: Path) -> tuple[zipfile.ZipInfo, ...]:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = tuple(archive.infolist())
            for member in infos:
                _safe_member_target(destination, member.filename)
            return infos
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
        raise InvalidRequestError(f"invalid Bohr download archive: {zip_path}") from exc


def _validate_archive(
    zip_path: Path, destination: Path
) -> tuple[zipfile.ZipInfo, ...]:
    infos = _archive_infos(zip_path, destination)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            bad_member = archive.testzip()
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
        raise InvalidRequestError(f"invalid Bohr download archive: {zip_path}") from exc
    if bad_member is not None:
        raise InvalidRequestError(
            f"corrupt Bohr download archive {zip_path}: member {bad_member} failed CRC"
        )
    return infos


def _safe_extract(zip_path: Path, destination: Path) -> tuple[str, ...]:
    extracted: list[str] = []
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = _safe_member_target(destination, member.filename)
            archive.extract(member, destination)
            if not member.is_dir():
                extracted.append(str(target))
    return tuple(extracted)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_fingerprint(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "archive_size": stat.st_size,
        "archive_mtime_ns": stat.st_mtime_ns,
        "archive_sha256": _sha256_file(path),
    }


def _read_archive_marker(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        # Empty markers were written by older TriCycle versions.  They are
        # migrated after the archive and extracted members pass validation.
        return None
    if not isinstance(value, dict) or value.get("version") != _DOWNLOAD_STATE_VERSION:
        return None
    return value


def _member_stats(
    destination: Path, infos: tuple[zipfile.ZipInfo, ...]
) -> list[dict[str, object]] | None:
    members: list[dict[str, object]] = []
    for member in infos:
        if member.is_dir():
            continue
        target = _safe_member_target(destination, member.filename)
        try:
            stat = target.stat()
        except OSError:
            return None
        if not target.is_file() or stat.st_size != member.file_size:
            return None
        members.append(
            {
                "path": member.filename,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return members


def _marker_members_are_current(
    destination: Path, state: dict[str, object]
) -> bool:
    raw_members = state.get("members")
    if not isinstance(raw_members, list):
        return False
    for raw_member in raw_members:
        if not isinstance(raw_member, dict):
            return False
        member_name = raw_member.get("path")
        expected_size = raw_member.get("size")
        expected_mtime = raw_member.get("mtime_ns")
        if not isinstance(member_name, str):
            return False
        if not isinstance(expected_size, int) or not isinstance(expected_mtime, int):
            return False
        target = _safe_member_target(destination, member_name)
        try:
            stat = target.stat()
        except OSError:
            return False
        if not target.is_file() or stat.st_size != expected_size or stat.st_mtime_ns != expected_mtime:
            return False
    return True


def _write_archive_marker(
    archive: Path,
    destination: Path,
    infos: tuple[zipfile.ZipInfo, ...],
) -> None:
    members = _member_stats(destination, infos)
    if members is None:
        raise InvalidRequestError(f"Bohr archive extraction is incomplete: {archive}")
    state = {
        "version": _DOWNLOAD_STATE_VERSION,
        "archive": archive.name,
        **_archive_fingerprint(archive),
        "members": members,
    }
    _atomic_write_text(
        archive.parent / _DOWNLOAD_MARKER,
        json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n",
    )


def _archive_is_current(archive: Path, destination: Path) -> bool:
    marker = archive.parent / _DOWNLOAD_MARKER
    state = _read_archive_marker(marker)
    if state is None:
        if not marker.exists():
            return False
        try:
            infos = _validate_archive(archive, destination)
            if _member_stats(destination, infos) is None:
                return False
            _write_archive_marker(archive, destination, infos)
            return True
        except (InvalidRequestError, OSError):
            return False

    try:
        _archive_infos(archive, destination)
        stat = archive.stat()
        if (
            state.get("archive") != archive.name
            or not isinstance(state.get("archive_sha256"), str)
            or not isinstance(state.get("archive_size"), int)
            or not isinstance(state.get("archive_mtime_ns"), int)
        ):
            return False
        if (
            stat.st_size != state["archive_size"]
            or stat.st_mtime_ns != state["archive_mtime_ns"]
        ):
            fingerprint = _archive_fingerprint(archive)
            if fingerprint["archive_sha256"] != state["archive_sha256"]:
                return False
            state.update(fingerprint)
            _atomic_write_text(
                marker,
                json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n",
            )
        return _marker_members_are_current(destination, state)
    except (InvalidRequestError, OSError):
        return False


def _extract_download_archives(destination: Path) -> tuple[str, ...]:
    extracted: list[str] = []
    pending: list[tuple[Path, tuple[zipfile.ZipInfo, ...]]] = []
    for archive in sorted(destination.rglob("out.zip")):
        if _archive_is_current(archive, archive.parent):
            continue
        pending.append((archive, _validate_archive(archive, archive.parent)))

    # Validate every archive before mutating the destination.  A bad archive
    # therefore cannot leave a second archive half-extracted beside it.
    for archive, infos in pending:
        extracted.extend(_safe_extract(archive, archive.parent))
        _write_archive_marker(archive, archive.parent, infos)
    return tuple(extracted)


def _payload_files(destination: Path) -> tuple[Path, ...]:
    metadata = {_DOWNLOAD_MANIFEST, _DOWNLOAD_MARKER}
    return tuple(
        path
        for path in sorted(destination.rglob("*"))
        if path.is_file() and path.name not in metadata
    )


def _manifest_is_current(destination: Path) -> bool:
    manifest = destination / _DOWNLOAD_MANIFEST
    if not manifest.is_file():
        return False
    try:
        entries = [
            line.strip() for line in manifest.read_text(encoding="utf-8").splitlines()
        ]
    except (OSError, UnicodeError):
        return False
    if not entries:
        return False
    root = destination.resolve()
    for entry in entries:
        target = (destination / entry).resolve()
        if target != root and root not in target.parents:
            return False
        if not target.is_file():
            return False
    return True


def _download_complete(
    destination: Path,
    *,
    unzip: bool,
    validate_archives: bool = True,
) -> bool:
    if not destination.is_dir() or not _payload_files(destination):
        return False
    archives = sorted(destination.rglob("out.zip"))
    if archives and validate_archives:
        if unzip:
            if not all(_archive_is_current(archive, archive.parent) for archive in archives):
                return False
        else:
            for archive in archives:
                _validate_archive(archive, archive.parent)
    return bool(archives) or _manifest_is_current(destination)


def _write_download_manifest(destination: Path) -> None:
    root = destination.resolve()
    entries = [
        str(path.resolve().relative_to(root)) for path in _payload_files(destination)
    ]
    if not entries:
        raise InvalidRequestError(f"Bohr download produced no files: {destination}")
    _atomic_write_text(destination / _DOWNLOAD_MANIFEST, "\n".join(entries) + "\n")


def _preserve_existing_download(destination: Path) -> Path | None:
    if not destination.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = destination.with_name(f"{destination.name}.replaced-{stamp}")
    suffix = 1
    while backup.exists():
        backup = destination.with_name(f"{destination.name}.replaced-{stamp}-{suffix}")
        suffix += 1
    destination.rename(backup)
    return backup


def _install_download(staging: Path, destination: Path) -> None:
    backup = _preserve_existing_download(destination)
    try:
        staging.rename(destination)
    except Exception:
        if backup is not None and not destination.exists():
            backup.rename(destination)
        raise


def _download_worker(
    gateway: BohrWorkflowGateway,
    external_id: str,
    destination: Path,
    output: Path,
    *,
    unzip: bool,
) -> None:
    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}.download-", dir=str(output)
    ) as temporary:
        staging = Path(temporary)
        gateway.download(external_id, staging)
        if unzip:
            _extract_download_archives(staging)
        else:
            for archive in sorted(staging.rglob("out.zip")):
                _validate_archive(archive, archive.parent)
        _write_download_manifest(staging)
        if not _download_complete(staging, unzip=unzip, validate_archives=False):
            raise InvalidRequestError(
                f"Bohr download did not produce a complete result: {destination}"
            )
        _install_download(staging, destination)


def download_run(
    run_dir: Path,
    gateway: BohrWorkflowGateway,
    *,
    only_succeeded: bool = False,
    continue_on_error: bool = False,
    resume: bool = False,
    unzip: bool = True,
) -> tuple[Path, LifecycleSummary]:
    """Download every submitted worker that has a job ID.

    ``FAILED`` is a worker-level status and does not imply that every task in
    that worker failed.  Keep ``only_succeeded`` as a compatibility argument,
    but do not use it to discard terminal worker results: partial logs and
    task outputs are often needed for recovery and diagnosis.
    """

    del only_succeeded
    output = run_dir / "downloads"
    output.mkdir(parents=True, exist_ok=True)
    job_group_id = _job_group_id_for_run(run_dir)
    rows: list[dict[str, object]] = []
    succeeded = failed = skipped = 0
    job_id_aliases = _read_job_id_aliases(run_dir)
    for source in read_submit_rows(run_dir):
        worker = source.get("worker", "")
        job_id = source.get("job_id", "")
        destination = output / f"{worker}-job-{job_id or 'none'}"
        status = ""
        if not job_id:
            status = "SKIPPED_NO_JOB_ID"
            skipped += 1
        else:
            # A worker-level FAILED result can still contain successful task
            # outputs. Attempt every submitted worker and let the download
            # report record whether the remote artifact is available.
            try:
                existing = destination.exists()
                complete = False
                validation_error: Exception | None = None
                if existing:
                    try:
                        if not destination.is_dir():
                            raise InvalidRequestError(
                                f"Bohr download destination is not a directory: {destination}"
                            )
                        if unzip:
                            _extract_download_archives(destination)
                        complete = _download_complete(destination, unzip=unzip)
                        if complete:
                            _write_download_manifest(destination)
                    except Exception as exc:
                        validation_error = exc

                if complete:
                    pass
                elif existing and (
                    not destination.is_dir() or any(destination.iterdir())
                ) and not resume:
                    detail = f" ({validation_error})" if validation_error else ""
                    raise InvalidRequestError(
                        f"existing Bohr download is incomplete or corrupt: {destination}{detail}; "
                        "rerun with resume=True to repair it"
                    ) from validation_error
                else:
                    resolved_job_id = _resolve_job_id(
                        run_dir,
                        gateway,
                        job_id,
                        job_group_id=job_group_id,
                        aliases=job_id_aliases,
                    )
                    _download_worker(
                        gateway,
                        resolved_job_id,
                        destination,
                        output,
                        unzip=unzip,
                    )
                status = "DOWNLOADED"
                succeeded += 1
            except Exception:
                status = "FAILED"
                failed += 1
                if not continue_on_error:
                    rows.append(
                        {
                            "worker": worker,
                            "tasks": source.get("tasks", ""),
                            "job_id": job_id,
                            "job_name": source.get("job_name", ""),
                            "status": status,
                            "download_dir": str(destination),
                        }
                    )
                    _write_tsv(output / "download-results.tsv", DOWNLOAD_FIELDS, rows)
                    raise
        rows.append(
            {
                "worker": worker,
                "tasks": source.get("tasks", ""),
                "job_id": job_id,
                "job_name": source.get("job_name", ""),
                "status": status,
                "download_dir": str(destination),
            }
        )
    result_file = output / "download-results.tsv"
    _write_tsv(result_file, DOWNLOAD_FIELDS, rows)
    return result_file, LifecycleSummary(len(rows), succeeded, failed, 0, skipped)


def terminate_run(
    run_dir: Path,
    gateway: BohrWorkflowGateway,
    *,
    confirm: bool,
    all_incomplete: bool = False,
    wanted_results: tuple[str, ...] = ("RUNNING", "PENDING"),
    wanted_workers: tuple[str, ...] = (),
) -> Path:
    if not confirm:
        raise InvalidRequestError("refusing to terminate jobs without explicit confirmation")
    status_file = run_dir / "status.tsv"
    if not status_file.is_file():
        status_file, _ = status_run(run_dir, gateway)
    wanted = {item.upper() for item in wanted_results}
    workers = set(wanted_workers)
    job_group_id = _job_group_id_for_run(run_dir)
    job_id_aliases = _read_job_id_aliases(run_dir)
    rows: list[dict[str, object]] = []
    for row in _read_tsv(status_file):
        if not row.get("job_id"):
            continue
        if workers and row.get("worker") not in workers:
            continue
        result = row.get("result", "").upper()
        selected = row.get("terminal") != "yes" if all_incomplete else result in wanted
        if not selected:
            continue
        action = "TERMINATED"
        exit_code = "0"
        try:
            resolved_job_id = _resolve_job_id(
                run_dir,
                gateway,
                row["job_id"],
                job_group_id=job_group_id,
                aliases=job_id_aliases,
            )
            output = gateway.terminate(resolved_job_id)
        except Exception as exc:
            action = "FAILED"
            exit_code = "1"
            output = str(exc)
        output_file = run_dir / "terminate-outputs" / f"{row['worker']}-job-{row['job_id']}.out"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(output, encoding="utf-8")
        rows.append(
            {
                "worker": row.get("worker", ""),
                "tasks": row.get("tasks", ""),
                "job_id": row.get("job_id", ""),
                "job_name": row.get("job_name", ""),
                "status_result": result,
                "action": action,
                "exit_code": exit_code,
                "output_file": str(output_file),
            }
        )
    result_file = run_dir / "terminate-results.tsv"
    _write_tsv(
        result_file,
        ("worker", "tasks", "job_id", "job_name", "status_result", "action", "exit_code", "output_file"),
        rows,
    )
    return result_file


def watch_run(
    run_dir: Path,
    gateway: BohrWorkflowGateway,
    *,
    interval: float = 60,
    max_iterations: int = 0,
    download_when_complete: bool = False,
) -> LifecycleSummary:
    if interval < 0 or max_iterations < 0:
        raise InvalidRequestError("watch interval and max_iterations must be non-negative")
    iteration = 0
    while True:
        iteration += 1
        _, summary = status_run(run_dir, gateway)
        if summary.incomplete == 0:
            if download_when_complete:
                download_run(run_dir, gateway, continue_on_error=True, resume=True)
            return summary
        if max_iterations and iteration >= max_iterations:
            raise TimeoutError(f"batch run did not finish after {iteration} status poll(s)")
        time.sleep(interval)
