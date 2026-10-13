"""Finite regression metrics shared by training, inference and publication."""

from __future__ import annotations

import math
from collections.abc import Sequence


def regression_metrics(
    targets: Sequence[float], predictions: Sequence[float]
) -> dict[str, float | int]:
    if not targets or len(targets) != len(predictions):
        raise ValueError("prediction count mismatch")
    if any(not math.isfinite(value) for values in (targets, predictions) for value in values):
        raise ValueError("non-finite target/prediction")
    errors = [prediction - target for target, prediction in zip(targets, predictions, strict=True)]
    if any(not math.isfinite(error) for error in errors):
        raise ValueError("non-finite prediction error")
    count = len(targets)
    squared = math.fsum(error * error for error in errors)
    mean = math.fsum(targets) / count
    denominator = math.fsum((target - mean) ** 2 for target in targets)
    result: dict[str, float | int] = {
        "n": count,
        "mae": math.fsum(abs(error) for error in errors) / count,
        "rmse": math.sqrt(squared / count),
        "bias": math.fsum(errors) / count,
    }
    # R² is undefined for a constant target; omit it rather than emit NaN/inf.
    if denominator:
        result["r2"] = 1 - squared / denominator
    if any(not math.isfinite(value) for value in result.values()):
        raise ValueError("non-finite regression metric")
    return result
