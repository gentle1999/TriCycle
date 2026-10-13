from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .errors import InvalidRequestError
from .io import atomic_write_text

TRACE_SCHEMA = "tricycle.trace/v1"


def _json_safe(value: object) -> object:
    """Convert the small set of values used by TriCycle to JSON-safe data."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidRequestError("trace payload contains a non-finite number")
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return [_json_safe(item) for item in sorted(value, key=str)]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_safe(to_dict())
    raise InvalidRequestError(f"trace payload contains unsupported value: {value!r}")


def canonical_json(value: object) -> str:
    """Return the canonical JSON representation used for trace hashes."""

    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_value(value: object) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One immutable, hash-chained event in a run ledger."""

    run_id: str
    event_type: str
    timestamp: str
    event_id: str
    seq: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    round_id: str | None = None
    step_id: str | None = None
    capability_id: str | None = None
    task_id: str | None = None
    attempt: int = 0
    status: str | None = None
    prev_hash: str = ""
    event_hash: str = ""
    schema: str = TRACE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != TRACE_SCHEMA:
            raise InvalidRequestError(f"unsupported trace schema: {self.schema}")
        if not self.run_id.strip():
            raise InvalidRequestError("trace run_id must not be empty")
        if not self.event_type.strip():
            raise InvalidRequestError("trace event_type must not be empty")
        if self.seq < 1:
            raise InvalidRequestError("trace sequence must be positive")
        if self.attempt < 0:
            raise InvalidRequestError("trace attempt must not be negative")

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "event_id": self.event_id,
            "seq": self.seq,
            "round_id": self.round_id,
            "step_id": self.step_id,
            "capability_id": self.capability_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "status": self.status,
            "payload": dict(self.payload),
            "prev_hash": self.prev_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "event_hash": self.event_hash}


@dataclass(frozen=True, slots=True)
class TraceVerification:
    valid: bool
    event_count: int
    last_seq: int
    last_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "event_count": self.event_count,
            "last_seq": self.last_seq,
            "last_hash": self.last_hash,
        }


class TraceSink(Protocol):
    """Minimal injection boundary for components that emit run events."""

    def emit(
        self,
        event_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        round_id: str | None = None,
        step_id: str | None = None,
        capability_id: str | None = None,
        task_id: str | None = None,
        attempt: int = 0,
        status: str | None = None,
    ) -> TraceEvent: ...


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            # The supported development/production environment is POSIX.  The
            # fallback keeps the ledger usable on platforms without fcntl.
            pass
        try:
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ImportError:
                pass


class TraceStore:
    """Append-only, hash-chained JSONL ledger for one run directory."""

    def __init__(
        self,
        run_dir: Path,
        *,
        run_id: str | None = None,
        clock: Callable[[], str] = _now,
        event_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self.run_dir = Path(run_dir).resolve()
        self.trace_dir = self.run_dir / "trace"
        self.events_path = self.trace_dir / "events.jsonl"
        self.lock_path = self.trace_dir / "events.lock"
        self.run_id = run_id
        self._clock = clock
        self._event_id_factory = event_id_factory

    def emit(
        self,
        event_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        round_id: str | None = None,
        step_id: str | None = None,
        capability_id: str | None = None,
        task_id: str | None = None,
        attempt: int = 0,
        status: str | None = None,
    ) -> TraceEvent:
        if not self.run_id or not self.run_id.strip():
            raise InvalidRequestError("TraceStore requires a non-empty run_id")
        normalized_payload = _json_safe(dict(payload or {}))
        if not isinstance(normalized_payload, dict):
            raise InvalidRequestError("trace payload must be a mapping")

        with _file_lock(self.lock_path):
            records = self._read_records_unlocked()
            previous = records[-1] if records else None
            seq = int(previous["seq"]) + 1 if previous else 1
            prev_hash = str(previous.get("event_hash", "")) if previous else ""
            event = TraceEvent(
                run_id=self.run_id,
                event_type=event_type,
                timestamp=self._clock(),
                event_id=self._event_id_factory(),
                seq=seq,
                payload=normalized_payload,
                round_id=round_id,
                step_id=step_id,
                capability_id=capability_id,
                task_id=task_id,
                attempt=attempt,
                status=status,
                prev_hash=prev_hash,
            )
            event_hash = sha256_value(event.unsigned_dict())
            event = replace(event, event_hash=event_hash)
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(canonical_json(event.to_dict()) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event

    append = emit

    def write_payload(self, name: str, value: object, *, suffix: str = ".json") -> dict[str, Any]:
        """Persist a potentially large payload and return a safe content reference."""

        safe_name = Path(name).name
        if not safe_name.strip():
            raise InvalidRequestError("trace payload name must not be empty")
        if suffix and not safe_name.endswith(suffix):
            safe_name += suffix
        content = canonical_json(value) + "\n"
        destination = self.trace_dir / "payloads" / safe_name
        atomic_write_text(destination, content)
        encoded = content.encode("utf-8")
        return {
            "path": str(destination.relative_to(self.run_dir)),
            "sha256": sha256_bytes(encoded),
            "size": len(encoded),
        }

    def read(self) -> tuple[dict[str, Any], ...]:
        with _file_lock(self.lock_path):
            return tuple(self._read_records_unlocked())

    def verify(self) -> TraceVerification:
        with _file_lock(self.lock_path):
            records = self._read_records_unlocked()
        previous_hash = ""
        expected_seq = 1
        for raw in records:
            if raw.get("schema") != TRACE_SCHEMA:
                raise InvalidRequestError("trace contains an unsupported schema")
            if raw.get("run_id") != self.run_id:
                raise InvalidRequestError("trace contains an unexpected run_id")
            if raw.get("seq") != expected_seq:
                raise InvalidRequestError("trace sequence is not contiguous")
            if raw.get("prev_hash", "") != previous_hash:
                raise InvalidRequestError(f"trace hash chain is broken at seq {expected_seq}")
            event_hash = raw.get("event_hash")
            unsigned = dict(raw)
            unsigned.pop("event_hash", None)
            if event_hash != sha256_value(unsigned):
                raise InvalidRequestError(f"trace event hash is invalid at seq {expected_seq}")
            previous_hash = str(event_hash)
            expected_seq += 1
        return TraceVerification(True, len(records), len(records), previous_hash)

    def _read_records_unlocked(self) -> list[dict[str, Any]]:
        if not self.events_path.is_file():
            return []
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
            records = [json.loads(line) for line in lines if line.strip()]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidRequestError(f"cannot read trace ledger: {self.events_path}") from exc
        if any(not isinstance(record, dict) for record in records):
            raise InvalidRequestError(f"trace ledger contains a non-object event: {self.events_path}")
        return records
