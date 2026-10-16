"""Validate and unpack result archives before writing any extracted artifact."""
from __future__ import annotations

import zipfile
import zlib
from pathlib import Path

from tricycle_core.errors import InvalidRequestError
from tricycle_core.transfer import validate_transfer_path


def safe_member_target(destination: Path, member_name: str) -> Path:
    root = destination.resolve()
    target = (destination / member_name).resolve()
    if not target.is_relative_to(root):
        raise InvalidRequestError(f"unsafe archive member: {member_name}")
    return target


def archive_infos(zip_path: Path, destination: Path) -> tuple[zipfile.ZipInfo, ...]:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = tuple(archive.infolist())
            for member in infos:
                validate_transfer_path(member.filename)
                safe_member_target(destination, member.filename)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise InvalidRequestError("symlink in downloaded archive")
            return infos
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
        raise InvalidRequestError(f"invalid Bohr download archive: {zip_path}") from exc


def validate_archive(zip_path: Path, destination: Path) -> tuple[zipfile.ZipInfo, ...]:
    infos = archive_infos(zip_path, destination)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            bad_member = archive.testzip()
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
        raise InvalidRequestError(f"invalid Bohr download archive: {zip_path}") from exc
    if bad_member is not None:
        raise InvalidRequestError(f"corrupt Bohr download archive {zip_path}: member {bad_member} failed CRC")
    return infos


def extract_archive(zip_path: Path, destination: Path) -> tuple[str, ...]:
    infos = validate_archive(zip_path, destination)
    extracted = []
    with zipfile.ZipFile(zip_path) as archive:
        for member in infos:
            target = safe_member_target(destination, member.filename)
            archive.extract(member, destination)
            if not member.is_dir():
                extracted.append(str(target))
    return tuple(extracted)
