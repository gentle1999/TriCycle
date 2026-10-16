"""Explicit I/O for logical task records and ordinary external asset paths.

No pathlib or built-in functions are patched. Callers opt into these operations;
external programs receive physical files only at declared materialization boundaries.
"""
from __future__ import annotations

import csv
import fnmatch
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import io as file_io
from .errors import InvalidRequestError
from .record_ledger import FILENAME, SCHEMA, RecordLedger
from .toml import render_toml


@lru_cache(maxsize=1024)
def _live(database: str, inode: int) -> bool:
    connection = sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
        return row is not None and row[0] == SCHEMA
    finally:
        connection.close()


def location(path: Path | str) -> tuple[RecordLedger, str, Path] | None:
    path = Path(os.path.abspath(path))
    if path.name == FILENAME or path.name.startswith(FILENAME + "-") or path.name.endswith(".lock"):
        return None
    for parent in (path, *path.parents):
        database = parent / FILENAME
        if database.is_file():
            actual = database.resolve()
            if _live(str(actual), actual.stat().st_ino):
                return RecordLedger(actual), path.relative_to(parent).as_posix(), parent
    return None


def _external(path: Path | str, found: tuple[RecordLedger, str, Path]) -> Path | None:
    ledger, name, root = found
    relative = Path(name)
    for ancestor in (relative, *relative.parents):
        if str(ancestor) == ".":
            continue
        entry = ledger.get(ancestor.as_posix())
        if entry is not None and entry["kind"] == "link":
            target = Path(entry["target"])
            if not target.is_absolute():
                target = root / ancestor.parent / target
            return target / relative.relative_to(ancestor)
    return None


def resolve(path: Path | str, *, strict: bool = False) -> Path:
    found = location(path)
    if found:
        external = _external(path, found)
        if external is not None:
            # Shared references point outside the live ledger. Never resolve a
            # cyclic logical link or silently materialize a record as a file.
            if location(external) is not None:
                raise ValueError("Live record links must reference external assets")
            return external.resolve(strict=strict)
        if strict and not exists(path):
            raise FileNotFoundError(path)
        return Path(path).resolve()
    return Path(path).resolve(strict=strict)


def exists(path: Path | str) -> bool:
    found = location(path)
    if not found:
        return Path(path).exists()
    if found[1] == ".":
        return True
    external = _external(path, found)
    return external.exists() if external is not None else found[0].get(found[1]) is not None


def is_file(path: Path | str) -> bool:
    found = location(path)
    if not found:
        return Path(path).is_file()
    external = _external(path, found)
    if found[1] == ".":
        return False
    return external.is_file() if external is not None else (found[0].get(found[1]) or {}).get("kind") == "file"


def is_dir(path: Path | str) -> bool:
    found = location(path)
    if not found:
        return Path(path).is_dir()
    if found[1] == ".":
        return True
    external = _external(path, found)
    return external.is_dir() if external is not None else (found[0].get(found[1]) or {}).get("kind") == "directory"


def is_symlink(path: Path | str) -> bool:
    found = location(path)
    return (found[0].get(found[1]) or {}).get("kind") == "link" if found and found[1] != "." else Path(path).is_symlink()


def read_bytes(path: Path | str) -> bytes:
    found = location(path)
    if not found:
        return Path(path).read_bytes()
    external = _external(path, found)
    return external.read_bytes() if external is not None else found[0].read(found[1])


def read_text(path: Path | str, encoding: str = "utf-8", errors: str = "strict") -> str:
    return read_bytes(path).decode(encoding, errors)


def _writable(path: Path | str) -> tuple[RecordLedger, str, Path] | None:
    found = location(path)
    if found and _external(path, found) is not None:
        raise ValueError("Cannot mutate a shared asset through a record reference")
    return found


def write_bytes(path: Path | str, data: bytes) -> int:
    found = _writable(path)
    if not found:
        return Path(path).write_bytes(data)
    found[0].put(found[1], data)
    return len(data)


def write_text(path: Path | str, data: str, encoding: str = "utf-8", errors: str = "strict", newline: str | None = None) -> int:
    if newline is not None:
        data = data.replace("\n", newline)
    write_bytes(path, data.encode(encoding, errors))
    return len(data)


def append_bytes(path: Path | str, data: bytes) -> None:
    found = _writable(path)
    if found:
        found[0].append(found[1], data)
    else:
        with Path(path).open("ab") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())


class _Writer(io.BytesIO):
    def __init__(self, path: Path | str, *, exclusive: bool) -> None:
        super().__init__()
        self.path = path
        self.exclusive = exclusive

    def close(self) -> None:
        if not self.closed:
            found = _writable(self.path)
            if found is None:
                raise ValueError("Record container disappeared during write")
            found[0].put(found[1], self.getvalue(), exclusive=self.exclusive)
        super().close()


class _Appender(io.RawIOBase):
    def __init__(self, path: Path | str) -> None:
        self.path = path

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        value = bytes(data)
        append_bytes(self.path, value)
        return len(value)


def open(path: Path | str, mode: str = "r", buffering: int = -1, encoding: str | None = None,
         errors: str | None = None, newline: str | None = None) -> Any:
    found = location(path)
    if not found:
        return Path(path).open(mode, buffering, encoding, errors, newline)
    external = _external(path, found)
    if external is not None:
        if mode not in {"r", "rb", "rt"}:
            raise ValueError("Cannot write through an immutable asset reference")
        return external.open(mode, buffering, encoding, errors, newline)
    if "+" in mode:
        raise ValueError("Use transactional record replacement instead of in-place updates")
    if mode[0] == "r":
        stream: Any = io.BytesIO(read_bytes(path))
    elif mode[0] == "a":
        stream = _Appender(path)
    elif mode[0] in {"w", "x"}:
        if mode[0] == "x" and exists(path):
            raise FileExistsError(path)
        stream = _Writer(path, exclusive=mode[0] == "x")
    else:
        raise ValueError(f"Unsupported record mode: {mode}")
    return stream if "b" in mode else io.TextIOWrapper(stream, encoding=encoding or "utf-8", errors=errors, newline=newline)


def mkdir(path: Path | str, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
    found = _writable(path)
    if found:
        found[0].mkdir(found[1], parents=parents, exist_ok=exist_ok, mode=mode)
    else:
        Path(path).mkdir(mode=mode, parents=parents, exist_ok=exist_ok)


def iterdir(path: Path | str) -> Iterator[Path]:
    path = Path(path)
    found = location(path)
    if not found:
        yield from path.iterdir()
        return
    external = _external(path, found)
    if external is not None:
        yield from (path / child.name for child in external.iterdir())
        return
    if not is_dir(path):
        raise NotADirectoryError(path)
    parent = "" if found[1] == "." else found[1] + "/"
    children = {name[len(parent):].split("/")[0] for name in found[0].entries() if name.startswith(parent) and name != found[1]}
    yield from (path / name for name in sorted(children))


def glob(path: Path | str, pattern: str) -> Iterator[Path]:
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        raise ValueError("Record glob must be a relative pattern")
    parts = Path(pattern).parts

    def walk(root: Path, pieces: tuple[str, ...]) -> Iterator[Path]:
        if not pieces:
            if exists(root):
                yield root
            return
        if not is_dir(root):
            return
        if pieces[0] == "**":
            yield from walk(root, pieces[1:])
            for child in iterdir(root):
                if is_dir(child) and not is_symlink(child):
                    yield from walk(child, pieces)
        else:
            for child in iterdir(root):
                if fnmatch.fnmatchcase(child.name, pieces[0]):
                    yield from walk(child, pieces[1:])
    yield from walk(Path(path), parts)


def rglob(path: Path | str, pattern: str) -> Iterator[Path]:
    yield from glob(path, "**/" + pattern)


def unlink(path: Path | str, missing_ok: bool = False) -> None:
    found = location(path)
    if found:
        entry = found[0].get(found[1])
        if entry and entry["kind"] == "directory":
            raise IsADirectoryError(path)
        found[0].remove(found[1], missing_ok=missing_ok)
    else:
        Path(path).unlink(missing_ok=missing_ok)


def rmtree(path: Path | str) -> None:
    found = location(path)
    if found:
        found[0].remove(found[1], recursive=True)
    else:
        shutil.rmtree(path)


def move(source: Path | str, destination: Path | str) -> Path:
    origin, target = location(source), location(destination)
    if origin or target:
        if not origin or not target or origin[0].key != target[0].key:
            raise ValueError("A record move requires one transaction in the same container")
        origin[0].move(origin[1], target[1])
    else:
        shutil.move(source, destination)
    return Path(destination)


def copyfile(source: Path | str, destination: Path | str) -> Path:
    if location(source) or location(destination):
        write_bytes(destination, read_bytes(source))
    else:
        shutil.copyfile(source, destination)
    return Path(destination)


def copy2(source: Path | str, destination: Path | str) -> Path:
    if location(source) or location(destination):
        return copyfile(source, destination)
    return Path(shutil.copy2(source, destination))


def symlink_to(path: Path | str, target: Path | str, target_is_directory: bool = False) -> None:
    found = location(path)
    if found:
        found[0].link(found[1], str(target))
    else:
        Path(path).symlink_to(target, target_is_directory=target_is_directory)


@contextmanager
def transaction(path: Path | str) -> Iterator[None]:
    found = location(path)
    if found:
        with found[0].transaction():
            yield
    else:
        yield


def atomic_write_bytes(path: Path, content: bytes) -> None:
    if location(path):
        with transaction(path):
            mkdir(path.parent, parents=True, exist_ok=True)
            write_bytes(path, content)
    else:
        file_io.atomic_write_bytes(path, content)


def atomic_write_text(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def write_json(path: Path, value: object) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n")


def file_sha256(path: Path | str) -> str:
    with open(path, "rb") as handle:
        checksum = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
        return checksum.hexdigest()


def write_delimited(path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, object]], *,
                    delimiter: str = ",", project_fields: bool = True) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(fields), delimiter=delimiter, lineterminator="\n")
    writer.writeheader()
    writer.writerows(({key: row.get(key, "") for key in fields} for row in rows) if project_fields else rows)
    atomic_write_text(path, buffer.getvalue())


def load_toml(path: Path) -> dict[str, Any]:
    if not is_file(path):
        raise InvalidRequestError(f"TOML file not found: {path}")
    try:
        return tomllib.loads(read_text(path))
    except tomllib.TOMLDecodeError as exc:
        raise InvalidRequestError(f"invalid TOML configuration: {path}") from exc


def dump_toml(path: Path, mapping: Mapping[str, object]) -> None:
    atomic_write_text(path, render_toml(mapping))
