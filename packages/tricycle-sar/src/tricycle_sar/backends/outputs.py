"""Export compact worker results without copying the training workspace."""

from __future__ import annotations

import argparse
import csv
import shutil
import tomllib
from collections import defaultdict
from pathlib import Path

try:
    from sar_runtime.io import write_delimited  # type: ignore[import-not-found]  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from sar_runtime.metrics import regression_metrics  # type: ignore[import-not-found]  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
except ImportError:
    from tricycle_core.io import write_delimited
    from tricycle_core.metrics import regression_metrics


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def write(path: Path, rows: list[dict]) -> None:
    if rows:
        write_delimited(path, list(rows[0]), rows, project_fields=False)


def collect(bundle: Path, task_path: Path, result: Path, exit_code: int) -> None:
    task = tomllib.loads(task_path.read_text())
    models = task.get("models", [task.get("model")])
    fold = int(task["fold"])
    result.mkdir(parents=True, exist_ok=True)
    predictions = {
        (row["model"], int(row["fold"]), row["reaction_id"]): row
        for row in read(result / "predictions.csv")
    }
    completed = set()
    for directory in ("smoke-results", "model-runs"):
        for path in sorted((bundle / directory).rglob("predictions.csv")):
            model = next((name for name in models if name in path.relative_to(bundle).parts), None)
            if model is None or f"fold_{fold}" not in path.parts:
                continue
            for source in read(path):
                row = {"model": model, "fold": str(fold), **source}
                key = model, fold, row["reaction_id"]
                if key in predictions and predictions[key] != row:
                    raise ValueError(f"Conflicting worker prediction: {key}")
                predictions[key] = row
            completed.add(model)
    if exit_code == 0 and completed != set(models):
        raise ValueError("Successful task is missing canonical model predictions")
    rows = [predictions[key] for key in sorted(predictions)]
    write(result / "predictions.csv", rows)
    groups = defaultdict(list)
    for row in rows:
        groups[row["model"], int(row["fold"])].append(row)
    metrics = []
    for (model, index), group in sorted(groups.items()):
        values = regression_metrics(
            [float(row["y_true"]) for row in group], [float(row["y_pred"]) for row in group]
        )
        metrics.append(
            {
                "model": model,
                "fold": index,
                "n_test": values["n"],
                "mae": values["mae"],
                "rmse": values["rmse"],
                "r2": values.get("r2", ""),
            }
        )
    write(result / "metrics.csv", metrics)
    runtime_fields = {"initialization_retries": 0, "failure_reason": "", "next_action": ""}
    status = {
        (row["model"], int(row["fold"])): {
            **row,
            **{key: row.get(key, value) for key, value in runtime_fields.items()},
        }
        for row in read(result / "status.csv")
    }
    environment_model = task.get("environment_model") or (
        "classical"
        if models[0].startswith("fingerprint_")
        else "chemprop"
        if models[0].startswith("chemprop_")
        else models[0]
    )
    for path in result.glob("*-state.toml"):
        state = tomllib.loads(path.read_text())
        if state.get("model") != environment_model:
            continue
        runtime_fields["initialization_retries"] = sum(
            entry["attempt"] > 1 for entry in state.get("initialization_attempts", [])
        )
        if exit_code and state.get("status") == "failed":
            runtime_fields.update(
                failure_reason=state.get("failure_reason", "initialization_failed"),
                next_action=state.get("next_action", "inspect_error"),
            )
    if exit_code and not runtime_fields["failure_reason"]:
        runtime_fields.update(failure_reason="execution_failed", next_action="inspect_error")
    for model in models:
        status[model, fold] = {
            "model": model,
            "fold": str(fold),
            "status": "complete" if model in completed and exit_code == 0 else "failed",
            "exit_code": exit_code,
            **runtime_fields,
        }
    write(result / "status.csv", [status[key] for key in sorted(status)])
    if task.get("retain_checkpoint", False):
        allowed = {".pt", ".pth", ".ckpt", ".safetensors", ".joblib", ".json", ".yaml", ".yml"}
        for checkpoint in (bundle / "model-runs").rglob("*"):
            if (
                checkpoint.is_file()
                and checkpoint.suffix in allowed
                and f"fold_{fold}" in checkpoint.parts
            ):
                target = result / "checkpoints" / checkpoint.relative_to(bundle / "model-runs")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(checkpoint, target)
    for path in result.iterdir():
        if path.name in {"predictions.csv", "metrics.csv", "status.csv", "checkpoints"}:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    args = parser.parse_args()
    collect(args.bundle, args.task, args.result, args.exit_code)


if __name__ == "__main__":
    main()
