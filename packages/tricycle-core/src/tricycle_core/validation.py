from collections.abc import Iterable, Mapping

from .errors import InvalidRequestError


def validate_fields(payload: Mapping[str, object], allowed: Iterable[str], *, context: str) -> None:
    """Reject undeclared request fields before defaults or side effects apply."""
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise InvalidRequestError(f"unknown {context} fields: {', '.join(unknown)}")
