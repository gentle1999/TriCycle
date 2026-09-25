from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .errors import InvalidRequestError


def timestamped_run_directory(
    run_root: Path,
    record_name: str,
    *,
    created_at: datetime | None = None,
) -> Path:
    """Build ``<root>/<date>/<timestamp>/<record>`` using local time."""

    if not record_name or record_name in {".", ".."}:
        raise InvalidRequestError("record_name must be a non-empty path component")
    if Path(record_name).name != record_name or "/" in record_name or "\\" in record_name:
        raise InvalidRequestError("record_name must be a single path component")

    timestamp = created_at or datetime.now().astimezone()
    timestamp = timestamp.astimezone()

    date_directory = timestamp.strftime("%Y-%m-%d")
    time_directory = timestamp.strftime("%H-%M-%S.%f%z")
    return Path(run_root) / date_directory / time_directory / record_name
