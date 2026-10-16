"""Lossless, indexed trajectory records; mutable execution stays in the workspace.

Sealing is an explicit finalization operation, never part of a scientific decision.
The single SQLite file contains compressed bytes, paths, hashes, modes and link
targets. Shared asset links are references, never recursively ingested.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from tricycle_core import record_io as rio
from tricycle_core.io import file_sha256
from tricycle_core.transfer import validate_transfer_path

FILENAME = "records.sqlite"
SCHEMA = "tricycle.replay-records/v1"


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _name(value: str) -> None:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or path.as_posix() != value
            or any(part in {".", ".."} for part in path.parts) or "\\" in value or "\x00" in value):
        raise ValueError(f"Unsafe record path: {value!r}")
    validate_transfer_path(value)
    if any(part == ".env" or part.startswith(".env.") for part in path.parts):
        raise ValueError(f"Environment configuration is not a trajectory record: {value}")


def _entries(root: Path) -> Iterator[tuple[dict[str, Any], bytes | None]]:
    def visit(directory: Path) -> Iterator[tuple[dict[str, Any], bytes | None]]:
        for path in sorted(directory.iterdir()):
            if path == root / FILENAME or path.name.endswith(".lock"):
                continue
            name = path.relative_to(root).as_posix()
            _name(name)
            before = path.lstat()
            entry: dict[str, Any] = {"path": name, "mode": stat.S_IMODE(before.st_mode)}
            data = None
            if stat.S_ISLNK(before.st_mode):
                entry.update(kind="link", target=os.readlink(path))
            elif stat.S_ISDIR(before.st_mode):
                entry.update(kind="directory")
            elif stat.S_ISREG(before.st_mode):
                data = path.read_bytes()
                after = path.lstat()
                if (after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode) != (
                        before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode):
                    raise ValueError(f"Record changed during sealing: {name}")
                entry.update(kind="file", sha256=_hash(data), size=len(data))
            else:
                raise ValueError(f"Unsupported record type: {name}")
            yield entry, data
            if entry["kind"] == "directory":
                yield from visit(path)
    yield from visit(root)


class RecordStore:
    """Read one immutable trajectory container without expanding its file tree."""

    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            header = dict(self.connection.execute("SELECT key, value FROM metadata"))
            if header["schema"] != SCHEMA:
                raise ValueError("Unsupported trajectory record schema")
            self.entries = {name: json.loads(value) for name, value in
                            self.connection.execute("SELECT path, metadata FROM entries ORDER BY path")}
            if _hash(_json(self.entries).encode()) != header["inventory_sha256"]:
                raise ValueError("Trajectory inventory hash mismatch")
            for name, entry in self.entries.items():
                _name(name)
                if entry["path"] != name or entry["kind"] not in {"file", "directory", "link"}:
                    raise ValueError("Invalid trajectory record entry")
                if not isinstance(entry["mode"], int) or not 0 <= entry["mode"] <= 0o777:
                    raise ValueError("Unsafe record permissions")
                for parent in PurePosixPath(name).parents:
                    if str(parent) != "." and self.entries.get(str(parent), {}).get("kind") != "directory":
                        raise ValueError(f"Record parent is not a directory: {name}")
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self) -> RecordStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.connection.close()

    def read(self, name: str) -> bytes:
        entry = self.entries[name]
        if entry["kind"] != "file":
            raise ValueError(f"Not a regular record: {name}")
        row = self.connection.execute("SELECT data FROM blobs WHERE sha256 = ?", (entry["sha256"],)).fetchone()
        if row is None:
            raise ValueError(f"Missing record bytes: {name}")
        data = zlib.decompress(row[0])
        if len(data) != entry["size"] or _hash(data) != entry["sha256"]:
            raise ValueError(f"Record content hash mismatch: {name}")
        return data

    def verify(self) -> dict[str, int]:
        if self.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Corrupt trajectory record database")
        count = size = 0
        for name, entry in self.entries.items():
            if entry["kind"] == "file":
                size += len(self.read(name))
                count += 1
        return {"files": count, "uncompressed_bytes": size, "entries": len(self.entries)}

    def restore(self, destination: Path, *, names: set[str] | None = None, links: bool = False) -> None:
        """Restore into a new directory; external links require explicit opt-in."""
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        wanted = set(self.entries) if names is None else names
        if wanted - self.entries.keys():
            raise ValueError("Requested record does not exist")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".records-", dir=destination.parent) as temporary:
            staging = Path(temporary) / "tree"
            staging.mkdir()
            directories = set()
            for name in sorted(wanted):
                entry = self.entries[name]
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if entry["kind"] == "file":
                    target.write_bytes(self.read(name))
                    target.chmod(entry["mode"])
                elif entry["kind"] == "directory":
                    target.mkdir(exist_ok=True)
                    directories.add(name)
                elif links:
                    target.symlink_to(entry["target"])
            for name in sorted(directories, reverse=True):
                (staging / name).chmod(self.entries[name]["mode"])
            # Destination is a caller-owned new view, never an existing run.
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(destination)
            staging.rename(destination)


def _check_complete(seed: Path) -> None:
    states = sorted(seed.parent.glob("seed-*/state.json"))
    if not states or seed / "state.json" not in states:
        raise ValueError("Expected a seed directory within a replay experiment")
    if any(json.loads(path.read_text())["phase"] != "complete" for path in states):
        raise ValueError("All experiment seeds must complete before sealing; active seeds may read pilot records")


def seal(seed: Path) -> dict[str, int]:
    """Publish, verify, then remove expanded records under the controller's lock.

    Repeating this operation finishes interrupted pruning. State and lock files
    remain available to status readers. Original bytes stay in the container.
    """
    found = rio.location(seed)
    if found:
        return found[0].verify()
    seed = seed.resolve()
    with (seed / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _check_complete(seed)
        output = seed / FILENAME
        if not output.exists():
            with tempfile.TemporaryDirectory(prefix="tricycle-records-") as temporary:
                database = Path(temporary) / FILENAME
                with sqlite3.connect(database) as connection:
                    connection.executescript("""
                        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                        CREATE TABLE entries (path TEXT PRIMARY KEY, metadata TEXT NOT NULL);
                        CREATE TABLE blobs (sha256 TEXT PRIMARY KEY, data BLOB NOT NULL);
                    """)
                    inventory = {}
                    for entry, data in _entries(seed):
                        inventory[entry["path"]] = entry
                        connection.execute("INSERT INTO entries VALUES (?, ?)", (entry["path"], _json(entry)))
                        if data is not None:
                            connection.execute("INSERT OR IGNORE INTO blobs VALUES (?, ?)",
                                               (entry["sha256"], zlib.compress(data)))
                    connection.executemany("INSERT INTO metadata VALUES (?, ?)", [
                        ("schema", SCHEMA), ("inventory_sha256", _hash(_json(inventory).encode())),
                        ("original_seed_directory", str(seed)),
                    ])
                connection.close()
                with RecordStore(database) as store:
                    store.verify()
                # Build locally; publish one closed file, not SQLite WAL writes on a mount.
                descriptor, pending_name = tempfile.mkstemp(prefix=f".{seed.name}-records-", dir=seed.parent)
                pending = Path(pending_name)
                try:
                    with database.open("rb") as source, os.fdopen(descriptor, "wb") as target:
                        shutil.copyfileobj(source, target)
                        target.flush()
                        os.fsync(target.fileno())
                    if file_sha256(database) != file_sha256(pending):
                        raise ValueError("Published record container differs from local bytes")
                    pending.replace(output)
                finally:
                    pending.unlink(missing_ok=True)
        for directory in (seed, seed.parent):
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        with RecordStore(output) as store:
            result = store.verify()
            # Preflight every remaining file before deleting any. Unknown/changed
            # evidence blocks cleanup, including after an interrupted publication.
            remaining = []
            for entry, _ in _entries(seed):
                if store.entries.get(entry["path"]) != entry:
                    raise ValueError(f"Expanded record differs from container: {entry['path']}")
                remaining.append(entry)
            if store.read("state.json") != (seed / "state.json").read_bytes():
                raise ValueError("Trajectory state changed after sealing")
            for entry in reversed(remaining):
                path = seed / entry["path"]
                if entry["path"] == "state.json":
                    continue
                if entry["kind"] == "directory":
                    # Locks are coordination state, not archived evidence.
                    if not any(path.iterdir()):
                        path.rmdir()
                else:
                    path.unlink()
            return {**result, "container_bytes": output.stat().st_size}


@contextmanager
def scientific_view(seed: Path) -> Iterator[Path]:
    """Expose only export inputs from a sealed seed in a disposable local view."""
    if rio.location(seed) or not (seed / FILENAME).exists():
        yield seed
        return
    if any(entry["kind"] != "directory" and entry["path"] != "state.json" for entry, _ in _entries(seed)):
        raise ValueError("Consolidation is incomplete; verify and rerun seal before exporting")
    with RecordStore(seed / FILENAME) as store, tempfile.TemporaryDirectory(prefix="tricycle-record-view-") as temporary:
        if store.read("state.json") != (seed / "state.json").read_bytes():
            raise ValueError("Sealed trajectory state changed")
        wanted = {name for name, entry in store.entries.items() if entry["kind"] == "file" and (
            name == "state.json" or name.endswith(("/evaluation-summary.json", "/result-asset-reference.json"))
            or "/decision/" in name and name.rsplit("/", 1)[-1] in {
                "task.json", "decision-input.json", "proposal-response.json", "review-response.json",
                "selection.json", "ACQUISITION.md", "acquisition-summary.json", "response.json"})}
        destination = Path(temporary) / seed.name
        store.restore(destination, names=wanted)
        yield destination


def watch(root: Path, *, live: bool, allow_submit: bool) -> list[dict[str, int]]:
    """Use the existing parallel controller, then seal only a completed experiment."""
    from .campaign import supervise

    seeds = sorted(path.parent for path in rio.glob(root, "seed-*/state.json"))
    if not seeds:
        raise ValueError("No replay seed states found")
    complete = all(json.loads(rio.read_text(seed / "state.json"))["phase"] == "complete" for seed in seeds)
    if not complete:
        supervise(root, live=live, allow_submit=allow_submit, once=False)
    if not live or any(json.loads(rio.read_text(seed / "state.json"))["phase"] != "complete" for seed in seeds):
        return []
    return [seal(seed) for seed in seeds]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sealing = commands.add_parser("seal", help="Consolidate a completed trajectory in place")
    sealing.add_argument("--seed-dir", type=Path, required=True)
    watching = commands.add_parser("watch", help="Supervise with the existing protocol, then consolidate completed records")
    watching.add_argument("--run-dir", type=Path, required=True)
    watching.add_argument("--live", action="store_true")
    watching.add_argument("--allow-submit", action="store_true")
    for command in ("verify", "list", "restore"):
        child = commands.add_parser(command)
        child.add_argument("--container", type=Path, required=True)
        if command == "restore":
            child.add_argument("--out-dir", type=Path, required=True)
            child.add_argument("--links", action="store_true", help="Recreate original shared asset links")
    args = parser.parse_args()
    if args.command == "seal":
        print(_json(seal(args.seed_dir)))
    elif args.command == "watch":
        print(_json(watch(args.run_dir, live=args.live, allow_submit=args.allow_submit)))
    else:
        with RecordStore(args.container) as store:
            if args.command == "verify":
                print(_json(store.verify()))
            elif args.command == "list":
                print("\n".join(store.entries))
            else:
                store.restore(args.out_dir, links=args.links)


if __name__ == "__main__":
    main()
