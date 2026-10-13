from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

import pandas as pd  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

try:
    from sar_runtime.io import file_sha256, write_json  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from sar_runtime.metrics import regression_metrics  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from sar_runtime.toml import dump_toml  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
except ImportError:
    from tricycle_core.io import file_sha256, write_json
    from tricycle_core.metrics import regression_metrics
    from tricycle_core.toml import dump_toml


REACTION_COLUMN = "rxn_smiles"
DEFAULT_TARGET = "G_T_activate"
WORKFLOW_RUN_SCHEMA_VERSION = "tricycle-kfold-workflow-run-v1"
DEFAULT_DOCKER_ENV = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}


class WorkflowRunError(RuntimeError):
    pass


@dataclass(frozen=True)
class FoldSplit:
    fold: int
    train: list[int]
    val: list[int]
    test: list[int]


@dataclass(frozen=True)
class WorkflowTask:
    model: str
    fold: FoldSplit
    gpu_device: str | None


@dataclass(frozen=True)
class WorkflowContext:
    workspace: Path
    snapshot: Path
    split_manifest: Path
    dataset: pd.DataFrame
    output: Path
    target: str
    seed: int
    timeout_seconds: int
    gpu_device: str | None
    reamvp_gpu: bool
    profile: str
    dry_run: bool

    def container_path(self, host_path: Path) -> str:
        resolved = host_path.resolve()
        try:
            rel_path = resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise WorkflowRunError(
                f"Path must be inside workspace {self.workspace}: {resolved}"
            ) from exc
        return "/workspace/" + rel_path.as_posix()


def profile_epochs(ctx: WorkflowContext) -> int:
    return 1 if ctx.profile == "quick" else 100


def profile_warmup_epochs(ctx: WorkflowContext) -> int:
    return 0 if ctx.profile == "quick" else 2


def profile_num_workers(ctx: WorkflowContext) -> int:
    return 0 if ctx.profile == "quick" else 4


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def run_command(
    command: list[str],
    *,
    log_path: Path,
    cwd: Path,
    timeout_seconds: int,
    dry_run: bool,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("$ " + " ".join(command) + "\n", encoding="utf-8")
    if dry_run:
        return {"command": command, "log": str(log_path), "returncode": 0, "skipped": True}

    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    with log_path.open("a", encoding="utf-8") as handle:
        process = subprocess.run(
            command,
            cwd=cwd,
            env=run_env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    if process.returncode != 0:
        raise WorkflowRunError(f"Command failed with exit code {process.returncode}: {log_path}")
    return {
        "command": command,
        "log": str(log_path),
        "returncode": process.returncode,
        "skipped": False,
    }


def gpu_args(ctx: WorkflowContext, *, use_gpu: bool) -> list[str]:
    if not use_gpu or ctx.gpu_device is None:
        return []
    return ["--gpus", f"device={ctx.gpu_device}"]


def docker_env_args(env: dict[str, str] | None = None) -> list[str]:
    merged_env = {**DEFAULT_DOCKER_ENV, **(env or {})}
    env_args: list[str] = []
    for key, value in merged_env.items():
        env_args.extend(["-e", f"{key}={value}"])
    return env_args


def docker_command(
    ctx: WorkflowContext,
    image: str,
    args: list[str],
    *,
    workdir: str = "/workspace",
    use_gpu: bool = False,
) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        *gpu_args(ctx, use_gpu=use_gpu),
        *docker_env_args(),
        "-v",
        f"{ctx.workspace}:/workspace",
        "-w",
        workdir,
        image,
        *args,
    ]


def docker_command_with_env(
    ctx: WorkflowContext,
    image: str,
    args: list[str],
    *,
    env: dict[str, str],
    workdir: str = "/workspace",
    use_gpu: bool = False,
) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        *gpu_args(ctx, use_gpu=use_gpu),
        *docker_env_args(env),
        "-v",
        f"{ctx.workspace}:/workspace",
        "-w",
        workdir,
        image,
        *args,
    ]


def load_snapshot_dataset(snapshot: Path) -> tuple[Path, pd.DataFrame]:
    manifest_path = snapshot / "manifest.json"
    if not manifest_path.exists():
        raise WorkflowRunError(f"Snapshot manifest does not exist: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    is_valid_snapshot = manifest.get("validation", {}).get("status") == "passed"
    is_full_fit_protocol = (
        manifest.get("protocol") == "full_fit_train_replay"
        and manifest.get("schema_version") == "tricycle-full-fit-replay-split-v1"
    )
    if not (is_valid_snapshot or is_full_fit_protocol):
        raise WorkflowRunError(f"Snapshot validation has not passed: {manifest_path}")
    dataset_path = snapshot / manifest.get("dataset", {}).get("path", "dataset.csv")
    if not dataset_path.exists():
        raise WorkflowRunError(f"Snapshot dataset does not exist: {dataset_path}")
    expected_sha256 = (
        manifest.get("dataset", {}).get("sha256")
        if is_valid_snapshot
        else manifest.get("dataset_sha256")
    )
    digest = file_sha256(dataset_path)
    if expected_sha256 != digest:
        raise WorkflowRunError(
            f"Snapshot dataset checksum does not match its manifest: {dataset_path}"
        )
    dataset = pd.read_csv(dataset_path)
    if int(manifest.get("row_count", -1)) != len(dataset):
        raise WorkflowRunError(
            f"Snapshot dataset row count does not match its manifest: {dataset_path}"
        )
    if "reaction_id" not in dataset.columns:
        dataset.insert(0, "reaction_id", [f"rxn-{idx:08d}" for idx in range(len(dataset))])
    dataset["row_index"] = np.arange(len(dataset), dtype=np.int64)
    return dataset_path, dataset


def load_kfold_splits(split_manifest: Path, *, max_folds: int | None = None) -> list[FoldSplit]:
    manifest = json.loads(split_manifest.read_text(encoding="utf-8"))
    if manifest.get("strategy") not in {"kfold", "group_kfold"}:
        raise WorkflowRunError(
            f"Split manifest is not a supported k-fold strategy: {split_manifest}"
        )
    indices_info = manifest.get("indices", {})
    indices_path = split_manifest.parent / indices_info.get("path", "indices.npz")
    if not indices_path.exists():
        raise WorkflowRunError(f"Split indices file does not exist: {indices_path}")

    npz = np.load(indices_path)
    folds = manifest.get("parameters", {}).get("folds")
    if not isinstance(folds, int) or folds <= 0:
        raise WorkflowRunError("Split manifest does not contain a valid folds parameter")
    if max_folds is not None:
        folds = min(folds, max_folds)

    result: list[FoldSplit] = []
    for fold in range(folds):
        result.append(
            FoldSplit(
                fold=fold,
                train=npz[f"fold_{fold}_train"].astype(int).tolist(),
                val=npz[f"fold_{fold}_val"].astype(int).tolist(),
                test=npz[f"fold_{fold}_test"].astype(int).tolist(),
            )
        )
    return result


def write_normalized_predictions(
    path: Path,
    df: pd.DataFrame,
    indices: list[int],
    target: str,
    y_pred: list[float],
) -> dict[str, Any]:
    if len(indices) != len(y_pred):
        raise WorkflowRunError(f"Prediction length mismatch: {len(y_pred)} != {len(indices)}")
    rows: list[dict[str, Any]] = []
    subset = df.iloc[indices]
    for (_, row), pred in zip(subset.iterrows(), y_pred, strict=True):
        rows.append(
            {
                "row_index": int(row["row_index"]),
                "reaction_id": row["reaction_id"],
                "target_name": target,
                "y_true": float(row[target]),
                "y_pred": float(pred),
            }
        )
    output = pd.DataFrame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(path, index=False)
    return compute_metrics_from_predictions(path, target)


def compute_metrics_from_predictions(predictions: Path, target: str) -> dict[str, Any]:
    df = pd.read_csv(predictions)
    y_true = df["y_true"].astype(float)
    y_pred = df["y_pred"].astype(float)
    shared_metrics = regression_metrics(y_true.tolist(), y_pred.tolist())
    metrics = {
        "schema_version": "tricycle-metrics-v1",
        "created_at_utc": utc_now(),
        "task_type": "regression",
        "target_column": target,
        "prediction_column": "y_pred",
        "predictions": str(predictions),
        "rows": int(len(df)),
        "metrics": {
            **{k: v for k, v in shared_metrics.items() if k in {"mae", "rmse", "r2"}},
            "mse": shared_metrics["rmse"] ** 2,
        },
    }
    metrics_path = predictions.with_name("metrics.json")
    write_json(metrics_path, metrics)
    return metrics


def read_first_numeric_column(path: Path, *, preferred: str | None = None) -> list[float]:
    df = pd.read_csv(path)
    if preferred and preferred in df.columns:
        return df[preferred].astype(float).tolist()
    for column in reversed(df.columns.tolist()):
        try:
            return df[column].astype(float).tolist()
        except (TypeError, ValueError):
            continue
    raise WorkflowRunError(f"No numeric prediction column found in {path}")


def write_toml(path: Path, sections: dict[str, dict[str, object]]) -> None:
    dump_toml(path, sections)


def summarize_model(results: list[dict[str, Any]]) -> dict[str, Any]:
    metric_rows = [result["metrics"]["metrics"] for result in results if result.get("metrics")]
    if not metric_rows:
        return {}
    summary: dict[str, Any] = {}
    for metric in ("mae", "mse", "rmse", "r2"):
        values = [float(row[metric]) for row in metric_rows]
        summary[metric] = {
            "mean": float(np.mean(values)),
            "std": float(np.std(values, ddof=0)),
            "values": values,
        }
    return summary
