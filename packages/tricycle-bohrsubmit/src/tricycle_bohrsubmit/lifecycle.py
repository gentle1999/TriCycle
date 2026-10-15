from __future__ import annotations

import csv
import json
import tempfile
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from tricycle_core import Artifact, BohrJobSpec
from tricycle_core.errors import InvalidRequestError
from tricycle_core.io import atomic_write_text as _atomic_write_text
from tricycle_core.io import file_sha256 as _sha256_file
from tricycle_core.io import write_delimited
from tricycle_core.protocols import BohrDownloadGateway, BohrStatusGateway

from .archives import archive_infos as _archive_infos
from .archives import extract_archive as _safe_extract
from .archives import safe_member_target as _safe_member_target
from .archives import validate_archive as _validate_archive
from .config import flatten_sections, load_toml
from .gateway import BohrWorkflowGateway
from .models import validate_chemistry_resources
from .training_protocol import worker_input

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
_DOWNLOAD_MANIFEST = "download-manifest.txt"
_DOWNLOAD_MARKER = ".bohr-download-unzipped"
_DOWNLOAD_STATE_VERSION = 1


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


def _write_tsv(path: Path, fields: tuple[str, ...], rows: Sequence[Mapping[str, object]]) -> None:
    write_delimited(path, fields, rows, delimiter="\t")


def _read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise InvalidRequestError(f"TSV file not found: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter="\t")]


def read_run_config(run_dir: Path) -> dict[str, object]:
    """Read the prepared batch TOML metadata."""
    path = run_dir / "batch.toml"
    if not path.is_file():
        raise InvalidRequestError(f"batch configuration not found: {path}")
    return flatten_sections(load_toml(path))


def read_submit_rows(run_dir: Path) -> list[dict[str, str]]:
    if (run_dir / "bohr-id-map.tsv").exists():
        raise InvalidRequestError("Internal-ID migration runs are unsupported; supply public Bohr IDs in a new request")
    return _read_tsv(run_dir / "submit-results.tsv")


def _run_list(config: dict[str, object], key: str) -> list[str]:
    value = config.get(key)
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value:
        return [value]
    return []


def validate_prepared_chemistry_resources(run_dir: Path) -> None:
    config = read_run_config(run_dir)
    engine = str(config.get("engine", ""))
    if not engine and not any(run_dir.glob("worker-*/dpa-berny.env")):
        return
    validate_chemistry_resources(
        engine,
        str(config.get("machine_type", "")),
        tuple(_run_list(config, "extra_submit_args")),
    )


def job_for_row(run_dir: Path, row: dict[str, str], *, job_group_id: str | None = None) -> BohrJobSpec:
    validate_prepared_chemistry_resources(run_dir)
    config = read_run_config(run_dir)
    worker = row.get("worker", "")
    input_dir = row.get("input_dir", "")
    if not input_dir:
        raise InvalidRequestError(f"submit row has no input_dir: {worker}")
    source = worker_input(run_dir, worker)
    is_dpa = (source / "work.sh").is_file() and (source / "dpa-berny.env").is_file()
    log_file = str(config.get("log_file", "worker.log"))
    command = f"bash {'work.sh' if is_dpa else 'run_worker.sh'} > {log_file} 2>&1"
    dataset_paths = _run_list(config, "dataset_paths")
    extra_submit_args = _run_list(
        config, "extra_submit_args"
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
    manifest = run_dir / worker / "input-assets.json"
    if manifest.is_file():
        expected = load_toml(run_dir / "batch.toml").get("input_assets", {}).get(worker)
        value = json.loads(manifest.read_text())
        if value != {"schema": "tricycle.asset-input/v1", "trees": [expected], "files": {}}:
            raise InvalidRequestError("Worker input manifest differs from batch record")
        parameters.pop("input_directory")
        parameters["input_assets_manifest"] = str(manifest)
    input_artifact = Artifact(
        artifact_id=f"{run_dir.name}-{worker}-input",
        location=str(manifest) if manifest.is_file() else input_dir,
        kind="manifest" if manifest.is_file() else "directory",
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


def status_run(run_dir: Path, gateway: BohrStatusGateway) -> tuple[Path, LifecycleSummary]:
    rows: list[dict[str, object]] = []
    query_failed = 0
    succeeded = failed = incomplete = skipped = 0
    checked_at = datetime.now(UTC).isoformat()
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
                remote = gateway.status(job_id)
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
    gateway: BohrDownloadGateway,
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
    gateway: BohrDownloadGateway,
    *,
    continue_on_error: bool = False,
    resume: bool = False,
    unzip: bool = True,
) -> tuple[Path, LifecycleSummary]:
    """Download every submitted worker that has a job ID.

    ``FAILED`` is a worker-level status and does not imply that every task in
    that worker failed. Partial logs and task outputs from every submitted
    worker are needed for recovery and diagnosis.
    """

    output = run_dir / "downloads"
    output.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    succeeded = failed = skipped = 0
    for source in read_submit_rows(run_dir):
        worker = source.get("worker", "")
        job_id = source.get("job_id", "")
        # Gateway IDs are opaque identifiers, not directory components.
        destination = output / f"{quote(worker, safe='')}-job-{quote(job_id or 'none', safe='')}"
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
                    _download_worker(
                        gateway,
                        job_id,
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
            output = gateway.terminate(row["job_id"])
        except Exception as exc:
            action = "FAILED"
            exit_code = "1"
            output = str(exc)
        output_name = f"{quote(row['worker'], safe='')}-job-{quote(row['job_id'], safe='')}.out"
        output_file = run_dir / "terminate-outputs" / output_name
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
