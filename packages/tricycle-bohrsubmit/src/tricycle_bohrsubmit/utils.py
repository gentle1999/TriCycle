from __future__ import annotations

import hashlib
import re
from pathlib import Path

from tricycle_core.errors import InvalidRequestError

_CKSUM_POLY = 0x04C11DB7


def posix_cksum(value: str) -> int:
    """Return the POSIX ``cksum`` value used by the legacy Bash scripts."""

    crc = 0xFFFFFFFF
    data = value.encode()
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ _CKSUM_POLY) & 0xFFFFFFFF if crc & 0x80000000 else (
                crc << 1
            ) & 0xFFFFFFFF

    length = len(data)
    while length:
        byte = length & 0xFF
        length >>= 8
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ _CKSUM_POLY) & 0xFFFFFFFF if crc & 0x80000000 else (
                crc << 1
            ) & 0xFFFFFFFF
    return crc ^ 0xFFFFFFFF


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def worker_label(index: int, workers: int) -> str:
    width = max(3, len(str(workers)))
    return f"worker-{index:0{width}d}"


def sanitize_name(name: str) -> str:
    stem = Path(name).stem
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem)
    return cleaned.strip("_") or "task"


def compact_name(name: str, max_length: int = 96) -> str:
    if max_length < 32:
        raise InvalidRequestError("job name limit must be at least 32")
    if len(name) <= max_length:
        return name
    suffix = short_hash(name)
    keep = max_length - len(suffix) - 1
    if keep < 8:
        raise InvalidRequestError("job name limit leaves too little room for a stable hash")
    return f"{name[:keep]}-{suffix}"
