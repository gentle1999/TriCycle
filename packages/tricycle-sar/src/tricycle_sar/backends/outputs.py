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



def checkpoint_files(model: str, fold_root: Path) -> list[Path]:
    """Select the final trained artifact locations owned by each current runner."""
    if model.startswith("fingerprint_"):
        names = ("model.joblib",)
    elif model in {"uam", "molclr", "reactiont5", "rxngraphormer"}:
        names = ("best.pt",)
    elif model == "rxnresid":
        names = ("checkpoint.pt",)
    elif model.startswith("chemprop_"):
        names = tuple(p.relative_to(fold_root).as_posix()
                      for p in (fold_root / "train").glob("model_*/best.pt"))
    elif model == "t5chem":
        names = ("model/pytorch_model.bin", "model/model.safetensors", "model/config.json",
                 "model/tokenizer.json", "model/tokenizer_config.json", "model/special_tokens_map.json",
                 "simple_vocab.txt")
    else:
        raise ValueError(f"No SAR result contract for model: {model}")
    files = [fold_root / name for name in names if (fold_root / name).is_file()]
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(fold_root.resolve()):
            raise ValueError("Checkpoint output points outside the trained fold")
    return files


def export_diagnostics(bundle: Path, result: Path, models: list[str], fold: int) -> None:
    names = ("training.json", "training_summary.json", "training_protocol.json", "history.json",
             "resolved_run.json", "accelerator-runtime.json", "native-runtime.json", "train.log")
    for root in (bundle / "model-runs", bundle / "smoke-results"):
        for directory in root.rglob(f"fold_{fold}"):
            if not any(model in directory.relative_to(root).parts for model in models):
                continue
            files = [directory / name for name in names if (directory / name).is_file()]
            files += list((directory / "logs").glob("*.log"))
            for path in files:
                if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                    raise ValueError("Diagnostic output escapes trained fold")
                target = result / "diagnostics" / path.relative_to(bundle)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)


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
    export_diagnostics(bundle, result, models, fold)
    if task.get("retain_checkpoint", False):
        root = bundle / "model-runs"
        for directory in root.rglob(f"fold_{fold}"):
            model = next((name for name in models if name in directory.relative_to(root).parts), None)
            if model is None:
                continue
            for checkpoint in checkpoint_files(model, directory):
                target = result / "checkpoints" / checkpoint.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(checkpoint, target)
    for path in result.iterdir():
        if path.name in {"predictions.csv", "metrics.csv", "status.csv", "checkpoints", "diagnostics"}:
            continue
        if path.is_file() and (path.suffix == ".log" or path.name.endswith((
            "-state.toml", "-pip-check.txt", "-pip-freeze.txt", "-environment-smoke.toml"
        ))):
            target = result / "diagnostics" / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(path, target)
        elif path.is_dir() and path.name == "stages":
            target = result / "diagnostics/stages"
            for diagnostic in path.iterdir():
                if diagnostic.is_file() and diagnostic.suffix in {".log", ".toml"}:
                    target.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(diagnostic, target / diagnostic.name)
            shutil.rmtree(path)
        elif path.is_dir():
            if path.name == f"{task.get('model', models[0])}-fold-{fold}":
                diagnostic = path / "accelerator.toml"
                if diagnostic.is_file():
                    target = result / "diagnostics" / path.name
                    target.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(diagnostic, target / diagnostic.name)
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
