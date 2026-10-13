"""Native RxnResid prediction validation and path-to-pair aggregation."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence

VARIANCE_FIELDS = ("predictive_variance", "epistemic_variance", "aleatoric_variance")
SIGNAL_FIELDS = (
    "prediction",
    *VARIANCE_FIELDS,
    "epistemic_std",
    "aleatoric_std",
    "predictive_std",
    "lower_95",
    "upper_95",
)


def numeric_signals(row: Mapping[str, object], *, required: Sequence[str] = ()) -> dict[str, float]:
    fields = set(required) | {name for name in SIGNAL_FIELDS if name in row}
    result = {}
    for name in fields:
        try:
            value = float(row[name])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"missing or non-numeric {name}") from exc
        if not math.isfinite(value):
            raise ValueError(f"non-finite {name}")
        if name.endswith(("_variance", "_std")) and value < 0:
            raise ValueError(f"negative {name}")
        result[name] = value
    return result


def identity(row: Mapping[str, object], columns: Sequence[str]) -> tuple[str, ...]:
    key = tuple("" if row.get(column) is None else str(row[column]).strip() for column in columns)
    if not key or any(not value for value in key):
        raise ValueError(f"missing prediction identity columns: {', '.join(columns)}")
    return key


def validate_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    expected: Sequence[Mapping[str, object]] | None = None,
    columns: Sequence[str] = ("path_id", "ene_id", "diene_id"),
    required: Sequence[str] = ("prediction", *VARIANCE_FIELDS),
    unique: bool = True,
) -> None:
    if not rows:
        raise ValueError("empty prediction output")
    keys = [identity(row, columns) for row in rows]
    if unique and len(keys) != len(set(keys)):
        raise ValueError("prediction output has duplicate identity")
    for row in rows:
        numeric_signals(row, required=required)
    if expected is not None:
        expected_keys = [identity(row, columns) for row in expected]
        if unique and len(expected_keys) != len(set(expected_keys)):
            raise ValueError("input has duplicate model identity")
        if Counter(keys) != Counter(expected_keys):
            raise ValueError("prediction coverage differs from input")


def aggregate_pairs(
    rows: Sequence[Mapping[str, object]],
    *,
    pair_key: Callable[[Mapping[str, object]], str] | None = None,
) -> dict[str, dict[str, float]]:
    validate_rows(rows, columns=("ene_id", "diene_id"), unique=False)
    grouped = defaultdict(list)
    for row in rows:
        key = pair_key(row) if pair_key else "+".join(identity(row, ("ene_id", "diene_id")))
        grouped[key].append(numeric_signals(row))
    return {
        key: {
            **{field: math.fsum(row[field] for row in entries) for field in VARIANCE_FIELDS},
            "prediction_mean": math.fsum(row["prediction"] for row in entries) / len(entries),
            "product_count": float(len(entries)),
        }
        for key, entries in grouped.items()
    }
