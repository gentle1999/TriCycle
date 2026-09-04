from __future__ import annotations

import os
import re
from collections.abc import Iterable, MutableMapping
from pathlib import Path

from .errors import InvalidRequestError

_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_DOUBLE_QUOTE_ESCAPES = {
    "\\": "\\",
    '"': '"',
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


def find_dotenv(start: Path | None = None) -> Path | None:
    """Find ``.env`` in *start* or one of its parent directories."""

    directory = Path.cwd() if start is None else Path(start).expanduser()
    if directory.is_file():
        directory = directory.parent
    for candidate_directory in (directory, *directory.parents):
        candidate = candidate_directory / ".env"
        if candidate.is_file():
            return candidate.resolve()
    return None


def _parse_value(raw_value: str, *, path: Path, line_number: int) -> str:
    value = raw_value.strip()
    if not value:
        return ""

    if value.startswith("'"):
        closing = value.find("'", 1)
        if closing == -1:
            raise InvalidRequestError(f"unterminated quoted value in {path}:{line_number}")
        trailing = value[closing + 1 :].strip()
        if trailing and not trailing.startswith("#"):
            raise InvalidRequestError(f"invalid trailing content in {path}:{line_number}")
        return value[1:closing]

    if value.startswith('"'):
        parsed: list[str] = []
        escaped = False
        for index, character in enumerate(value[1:], start=1):
            if escaped:
                parsed.append(_DOUBLE_QUOTE_ESCAPES.get(character, character))
                escaped = False
                continue
            if character == "\\":
                escaped = True
                continue
            if character == '"':
                trailing = value[index + 1 :].strip()
                if trailing and not trailing.startswith("#"):
                    raise InvalidRequestError(f"invalid trailing content in {path}:{line_number}")
                return "".join(parsed)
            parsed.append(character)
        if escaped:
            parsed.append("\\")
        raise InvalidRequestError(f"unterminated quoted value in {path}:{line_number}")

    comment = re.search(r"\s+#", value)
    if comment is not None:
        value = value[: comment.start()].rstrip()
    return value


def _parse_line(
    line: str,
    *,
    path: Path,
    line_number: int,
    allowed_keys: frozenset[str] | None = None,
) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export") and (
        len(stripped) == len("export") or stripped[len("export")].isspace()
    ):
        stripped = stripped[len("export") :].lstrip()
    if "=" not in stripped:
        raise InvalidRequestError(f"expected KEY=VALUE in {path}:{line_number}")
    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    if _ENV_KEY.fullmatch(key) is None:
        raise InvalidRequestError(f"invalid environment variable name in {path}:{line_number}")
    if allowed_keys is not None and key not in allowed_keys:
        return key, ""
    return key, _parse_value(raw_value, path=path, line_number=line_number)


def load_dotenv(
    path: str | Path | None = None,
    *,
    override: bool = False,
    keys: Iterable[str] | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> Path | None:
    """Load a local ``.env`` file into an environment mapping.

    When *path* is omitted, the current directory and its parents are searched.
    Existing environment variables win by default.  *keys* can restrict which
    entries are read; Bohr request models use this to load non-secret defaults
    without loading credentials during a dry-run.
    """

    dotenv_path = Path(path).expanduser().resolve() if path is not None else find_dotenv()
    if dotenv_path is None or not dotenv_path.is_file():
        return None

    target = os.environ if environ is None else environ
    allowed_keys = None if keys is None else frozenset(keys)
    with dotenv_path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            parsed = _parse_line(
                line,
                path=dotenv_path,
                line_number=line_number,
                allowed_keys=allowed_keys,
            )
            if parsed is None:
                continue
            key, value = parsed
            if allowed_keys is not None and key not in allowed_keys:
                continue
            if override or key not in target:
                target[key] = value
    return dotenv_path
