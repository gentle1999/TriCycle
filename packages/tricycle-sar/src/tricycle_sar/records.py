"""Compact SAR records: shared definitions, task differences, and portable results."""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from pathlib import Path

from tricycle_core.training_protocol import training_method


def compact_tasks(rows: Sequence[Mapping]) -> tuple[dict, list[dict]]:
    """Factor identical task fields once, including nested hashes and configuration."""
    if not rows:
        raise ValueError("An experiment needs at least one task")
    if any(not isinstance(row, Mapping) or not row.get("name") for row in rows):
        raise ValueError("Every task needs a name")
    names = [row["name"] for row in rows]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate task name")
    shared = {
        key: value
        for key, value in rows[0].items()
        if key != "name" and all(key in row and row[key] == value for row in rows)
    }
    differences = [{key: value for key, value in row.items() if key not in shared} for row in rows]
    return shared, differences


def read_method_bindings(path: Path) -> dict[tuple[str, int], str]:
    """The grouped method table is the sole per-fold method binding."""
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    result = {}
    seen_methods = set()
    for row in rows:
        model, method = row["model"], row["method_id"]
        if (model, method) in seen_methods:
            raise ValueError("Method bindings must group identical methods into one row")
        seen_methods.add((model, method))
        for fold in row["folds"].split(","):
            if fold != str(int(fold)) or int(fold) < 0:
                raise ValueError("Invalid fold binding")
            key = model, int(fold)
            if key in result:
                raise ValueError("Duplicate fold method binding")
            result[key] = method
    if not result:
        raise ValueError("Empty method bindings")
    return result


def write_method_bindings(path: Path, bindings: Mapping, manifest: Path, registry: dict) -> None:
    for (model, _), method_id in bindings.items():
        training_method(manifest, registry, model, method_id)
    write_recorded_bindings(path, bindings)


def write_recorded_bindings(path: Path, bindings: Mapping) -> None:
    """Serialize observed method identities without loading executable backends."""
    import re

    groups: dict[tuple[str, str], list[int]] = {}
    for (model, fold), method_id in sorted(bindings.items()):
        if re.fullmatch(re.escape(model) + r"/train/[0-9a-f]{16}", method_id) is None:
            raise ValueError("Invalid recorded method identity")
        groups.setdefault((model, method_id), []).append(fold)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["model", "folds", "method_id"],
        )
        writer.writeheader()
        for (model, method_id), folds in groups.items():
            writer.writerow(
                {
                    "model": model,
                    "folds": ",".join(map(str, folds)),
                    "method_id": method_id,
                }
            )


def publication_files(root: Path) -> list[Path]:
    """Publication excludes runtime evidence and duplicate prediction tables."""
    allowed = {"data", "results", "figures"}
    root_files = {
        "README.md",
        "REPRODUCIBILITY.md",
        "RESULTS_REPORT.md",
        "manifest.json",
        "METHOD_BINDINGS.csv",
        "SHA256SUMS",
    }
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if {"__pycache__", ".venv"}.intersection(relative.parts):
            continue
        if not (relative.parts[0] in allowed or relative.as_posix() in root_files):
            raise ValueError(
                f"Runtime evidence does not belong in a SAR result package: {relative}"
            )
        if relative.parts[0] in {"data", "results"} and path.suffix != ".csv":
            raise ValueError(f"Runtime evidence does not belong in scientific tables: {relative}")
        if relative.parts[0] == "results" and relative.as_posix() not in {
            "results/oof-predictions.csv",
            "results/fold-metrics.csv",
            "results/model-summary.csv",
        }:
            raise ValueError(f"Duplicate or unexpected result table: {relative}")
        if relative.parts[0] == "figures" and path.suffix not in {".png", ".pdf", ".svg"}:
            raise ValueError(f"Unexpected figure artifact: {relative}")
        files.append(path)
    return files
