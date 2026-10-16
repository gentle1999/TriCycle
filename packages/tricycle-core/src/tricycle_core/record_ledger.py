"""Transactional live records with content checks and an append-only audit chain.

SQLite files belong on local durable storage. Publish consistent copies using
``backup``; never synchronize a live database's WAL files independently.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import Any

from .transfer import validate_transfer_path

SCHEMA = "tricycle.live-records/v1"
FILENAME = "records.sqlite"
_CONNECTIONS: ContextVar[dict[str, sqlite3.Connection] | None] = ContextVar("record_transactions", default=None)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def valid_name(name: str) -> None:
    path = PurePosixPath(name)
    if (not name or name == "." or path.is_absolute() or path.as_posix() != name or ".." in path.parts
            or "\\" in name or "\x00" in name):
        raise ValueError(f"Unsafe record key: {name!r}")
    validate_transfer_path(name)
    if any(p == ".env" or p.startswith(".env.") for p in path.parts):
        raise ValueError("Environment configuration is not a task record")


class RecordLedger:
    """One durable namespace. Each mutation and its audit entry commit together."""

    def __init__(self, database: Path) -> None:
        self.database = database.resolve()
        self.key = str(self.database)

    @classmethod
    def create(cls, database: Path) -> RecordLedger:
        database.parent.mkdir(parents=True, exist_ok=True)
        with database.open("xb"):
            pass
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE entries (path TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    sha256 TEXT, size INTEGER NOT NULL, mode INTEGER NOT NULL, target TEXT);
                CREATE INDEX entry_blobs ON entries(sha256);
                CREATE TABLE blobs (sha256 TEXT PRIMARY KEY, data BLOB NOT NULL);
                CREATE TABLE events (sequence INTEGER PRIMARY KEY, previous TEXT NOT NULL,
                    sha256 TEXT NOT NULL, payload TEXT NOT NULL);
            """)
            connection.execute("INSERT INTO metadata VALUES ('schema', ?)", (SCHEMA,))
            connection.commit()
        finally:
            connection.close()
        return cls(database)

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        active = (_CONNECTIONS.get() or {}).get(self.key)
        if active is not None:
            yield active
            return
        connection = sqlite3.connect(self.database.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=30)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            row = connection.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
            if row is None or row[0] != SCHEMA:
                raise ValueError("Unsupported live record schema")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            token = _CONNECTIONS.set({**(_CONNECTIONS.get() or {}), self.key: connection})
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                _CONNECTIONS.reset(token)
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.connection(write=True) as connection:
            name = "tx_" + uuid.uuid4().hex
            connection.execute(f"SAVEPOINT {name}")
            try:
                yield
            except BaseException:
                connection.execute(f"ROLLBACK TO {name}")
                raise
            finally:
                connection.execute(f"RELEASE {name}")

    @staticmethod
    def _entry(row: tuple[Any, ...]) -> dict[str, Any]:
        return dict(zip(("path", "kind", "sha256", "size", "mode", "target"), row, strict=True))

    def entries(self) -> dict[str, dict[str, Any]]:
        with self.connection() as connection:
            return {row[0]: self._entry(row) for row in connection.execute("SELECT * FROM entries ORDER BY path")}

    def get(self, name: str) -> dict[str, Any] | None:
        valid_name(name)
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM entries WHERE path=?", (name,)).fetchone()
            return None if row is None else self._entry(row)

    def read(self, name: str) -> bytes:
        with self.connection() as connection:
            entry = self.get(name)
            if entry is None:
                raise FileNotFoundError(name)
            if entry["kind"] != "file":
                raise IsADirectoryError(name)
            row = connection.execute("SELECT data FROM blobs WHERE sha256=?", (entry["sha256"],)).fetchone()
            if row is None:
                raise ValueError(f"Missing record content: {name}")
            data = zlib.decompress(row[0])
            if len(data) != entry["size"] or digest(data) != entry["sha256"]:
                raise ValueError(f"Corrupt record content: {name}")
            return data

    def _change(self, changes: list[tuple[str, dict[str, Any] | None]], action: str) -> None:
        with self.connection(write=True) as connection:
            mutations = []
            for name, after in changes:
                valid_name(name)
                before = self.get(name)
                if before == after:
                    continue
                if after is None:
                    connection.execute("DELETE FROM entries WHERE path=?", (name,))
                else:
                    connection.execute("INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?, ?, ?)",
                                       tuple(after[k] for k in ("path", "kind", "sha256", "size", "mode", "target")))
                mutations.append({"path": name, "before": before, "after": after})
            if mutations:
                previous = connection.execute("SELECT sequence, sha256 FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
                sequence, parent = (previous[0] + 1, previous[1]) if previous else (1, "0" * 64)
                payload = canonical({"sequence": sequence, "previous": parent, "at_ns": time.time_ns(),
                                     "action": action, "changes": mutations})
                connection.execute("INSERT INTO events VALUES (?, ?, ?, ?)", (sequence, parent, digest(payload.encode()), payload))
            # Keep current mutable state once. Immutable attempts retain their own
            # keys; the journal retains prior identities, not repeated full states.
            connection.execute("DELETE FROM blobs WHERE NOT EXISTS (SELECT 1 FROM entries WHERE entries.sha256=blobs.sha256)")

    def mkdir(self, name: str, *, parents: bool = False, exist_ok: bool = False, mode: int = 0o755) -> None:
        if name in {"", "."}:
            return
        valid_name(name)
        with self.transaction():
            previous = self.get(name)
            if previous is not None:
                if exist_ok and previous["kind"] == "directory":
                    return
                raise FileExistsError(name)
            parent = str(PurePosixPath(name).parent)
            if parent != ".":
                if parents:
                    self.mkdir(parent, parents=True, exist_ok=True)
                elif (self.get(parent) or {}).get("kind") != "directory":
                    raise FileNotFoundError(parent)
            self._change([(name, {"path": name, "kind": "directory", "sha256": None,
                                  "size": 0, "mode": mode, "target": None})], "mkdir")

    def put(self, name: str, data: bytes, *, exclusive: bool = False, mode: int | None = None) -> None:
        valid_name(name)
        with self.transaction(), self.connection(write=True) as connection:
            previous = self.get(name)
            if previous and (exclusive or previous["kind"] != "file"):
                raise FileExistsError(name)
            parent = str(PurePosixPath(name).parent)
            if parent != "." and (self.get(parent) or {}).get("kind") != "directory":
                raise FileNotFoundError(parent)
            checksum = digest(data)
            connection.execute("INSERT OR IGNORE INTO blobs VALUES (?, ?)", (checksum, zlib.compress(data)))
            self._change([(name, {"path": name, "kind": "file", "sha256": checksum,
                                  "size": len(data), "mode": mode if mode is not None else previous["mode"] if previous else 0o644, "target": None})], "write")

    def append(self, name: str, data: bytes) -> None:
        with self.transaction():
            previous = self.get(name)
            self.put(name, (self.read(name) if previous else b"") + data,
                     mode=previous["mode"] if previous else 0o644)

    def link(self, name: str, target: str) -> None:
        valid_name(name)
        with self.transaction():
            if self.get(name):
                raise FileExistsError(name)
            self.mkdir(str(PurePosixPath(name).parent), parents=True, exist_ok=True)
            self._change([(name, {"path": name, "kind": "link", "sha256": None,
                                  "size": 0, "mode": 0o777, "target": target})], "link")

    def remove(self, name: str, *, recursive: bool = False, missing_ok: bool = False) -> None:
        with self.transaction():
            entries = self.entries()
            if name not in entries:
                if missing_ok:
                    return
                raise FileNotFoundError(name)
            children = [key for key in entries if key.startswith(name + "/")]
            if children and not recursive:
                raise IsADirectoryError(name)
            self._change([(key, None) for key in [name, *children]], "remove")

    def move(self, source: str, destination: str) -> None:
        valid_name(destination)
        if destination == source or destination.startswith(source + "/"):
            raise ValueError("Cannot move a record inside itself")
        with self.transaction():
            entries = self.entries()
            if source not in entries:
                raise FileNotFoundError(source)
            if destination in entries:
                raise FileExistsError(destination)
            self.mkdir(str(PurePosixPath(destination).parent), parents=True, exist_ok=True)
            selected = [k for k in entries if k == source or k.startswith(source + "/")]
            changes = []
            for key in selected:
                new = destination + key[len(source):]
                changes.extend([(key, None), (new, {**entries[key], "path": new})])
            self._change(changes, "move")

    def verify(self) -> dict[str, Any]:
        with self.connection() as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Record database integrity check failed")
            expected: dict[str, Any] = {}
            parent = "0" * 64
            count = 0
            for sequence, previous, checksum, encoded in connection.execute("SELECT * FROM events ORDER BY sequence"):
                count += 1
                event = json.loads(encoded)
                if (sequence != count or previous != parent or digest(encoded.encode()) != checksum
                        or event["sequence"] != sequence or event["previous"] != parent):
                    raise ValueError("Record audit chain changed")
                for change in event["changes"]:
                    name = change["path"]
                    if expected.get(name) != change["before"]:
                        raise ValueError("Record audit history is discontinuous")
                    if change["after"] is None:
                        expected.pop(name)
                    else:
                        expected[name] = change["after"]
                parent = checksum
            entries = self.entries()
            if expected != entries:
                raise ValueError("Current records differ from audited history")
            size = files = 0
            for name, entry in entries.items():
                valid_name(name)
                for ancestor in PurePosixPath(name).parents:
                    if str(ancestor) != "." and entries.get(str(ancestor), {}).get("kind") != "directory":
                        raise ValueError("Record parent is not a directory")
                if entry["kind"] == "file":
                    size += len(self.read(name))
                    files += 1
            return {"schema": SCHEMA, "files": files, "entries": len(entries), "uncompressed_bytes": size,
                    "events": count, "head_sha256": parent}

    def backup(self, destination: Path) -> None:
        """Create a consistent standalone database, including committed WAL data."""
        if self.key in (_CONNECTIONS.get() or {}):
            raise ValueError("Publish snapshots outside an active record transaction")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".snapshot-", dir=destination.parent)
        os.close(descriptor)
        pending = Path(name)
        try:
            target = sqlite3.connect(pending)
            source = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
            try:
                source.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")
            finally:
                source.close()
                target.close()
            RecordLedger(pending).verify()
            with pending.open("rb") as handle:
                os.fsync(handle.fileno())
            # Publish without replacing an existing evidence snapshot.
            os.link(pending, destination)
            descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            pending.unlink(missing_ok=True)
