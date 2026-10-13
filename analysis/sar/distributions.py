"""Descriptive GT distributions; independent of model and task execution."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict

from tricycle_sar.predictions import identity
from tricycle_sar.results import close


def activation_distribution_data(rows: list[dict[str, str]], target: str) -> dict:
    """Separate path deviations and equally weighted substrate-group means."""
    if not rows:
        raise ValueError("Empty activation dataset")
    groups = defaultdict(list)
    values, keys, seen = [], [], set()
    for row in rows:
        value = float(row[target])
        key = identity(row, ("ene_id", "diene_id"))
        if not math.isfinite(value) or row["path_id"] in seen:
            raise ValueError("Nonfinite activation or duplicate path ID")
        seen.add(row["path_id"])
        groups[key].append(value)
        values.append(value)
        keys.append(key)
    means = {key: statistics.fmean(group) for key, group in groups.items()}
    path_mean = statistics.fmean(values)
    group_mean = statistics.fmean(means.values())
    residuals = [value - means[key] for value, key in zip(values, keys, strict=True)]
    multiple = [r for r, key in zip(residuals, keys, strict=True) if len(groups[key]) > 1]
    within = statistics.fmean(r * r for r in residuals)
    between = statistics.fmean((means[key] - path_mean) ** 2 for key in keys)
    total = statistics.pvariance(values)
    close(within + between, total, 1e-10 * max(1, total), "Variance decomposition")
    return {
        "values": values,
        "within_group_residuals": residuals,
        "multi_path_residuals": multiple,
        "group_mean_residuals": [mean - group_mean for mean in means.values()],
        "summary": {
            "paths": len(values),
            "groups": len(groups),
            "singleton_groups": sum(len(group) == 1 for group in groups.values()),
            "path_weighted_mean": path_mean,
            "equal_group_mean": group_mean,
            "ddof": 0,
            "equal_group_mean_within_variance": statistics.fmean(
                statistics.pvariance(group) for group in groups.values()
            ),
            "equal_group_between_variance": statistics.pvariance(means.values()),
            "path_weighted_variance_decomposition": {
                "within": within,
                "between": between,
                "total": total,
                "between_fraction": between / total if total else None,
            },
        },
    }
