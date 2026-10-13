from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
from scripts.tricycle_model_adapters.workflow_common import REACTION_COLUMN, FoldSplit, WorkflowContext, docker_command, docker_command_with_env, profile_epochs, read_first_numeric_column, run_command, write_normalized_predictions  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip


def write_t5chem_files(path: Path, df: pd.DataFrame, indices: list[int], target: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subset = df.iloc[indices]
    (path / "source").write_text(
        "".join(f"{value}\n" for value in subset[REACTION_COLUMN].astype(str)),
        encoding="utf-8",
    )
    (path / "target").write_text(
        "".join(f"{value}\n" for value in subset[target].astype(str)),
        encoding="utf-8",
    )


def write_t5chem_split_files(root: Path, df: pd.DataFrame, split: FoldSplit, target: str) -> None:
    for split_name, indices in (("train", split.train), ("val", split.val), ("test", split.test)):
        split_dir = root / split_name
        write_t5chem_files(split_dir, df, indices, target)
        (root / f"{split_name}.source").write_text(
            (split_dir / "source").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (root / f"{split_name}.target").write_text(
            (split_dir / "target").read_text(encoding="utf-8"), encoding="utf-8"
        )


def write_simple_vocab(path: Path, df: pd.DataFrame, target: str) -> None:
    base_tokens = ["<s>", "</s>", "<unk>", "<pad>", "<mask>"]
    chars: set[str] = set()
    for value in df[REACTION_COLUMN].astype(str):
        chars.update(value)
    for value in df[target].astype(str):
        chars.update(value)
    tokens = [*base_tokens, *sorted(chars - set(base_tokens))]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{token}\n" for token in tokens), encoding="utf-8")


def run_t5chem_fold(ctx: WorkflowContext, split: FoldSplit) -> dict[str, Any]:
    fold_root = ctx.output / "t5chem" / f"fold_{split.fold}"
    data_dir = fold_root / "data"
    model_dir = fold_root / "model"
    write_t5chem_split_files(data_dir, ctx.dataset, split, ctx.target)
    vocab_path = fold_root / "simple_vocab.txt"
    write_simple_vocab(vocab_path, ctx.dataset.iloc[split.train], ctx.target)
    commands: list[dict[str, Any]] = []
    train_args = [
        "train",
        "--data_dir",
        ctx.container_path(data_dir),
        "--output_dir",
        ctx.container_path(model_dir),
        "--task_type",
        "regression",
        "--run_name",
        f"kfold_{ctx.target}_fold_{split.fold}",
        "--tokenizer",
        "simple",
        "--vocab",
        ctx.container_path(vocab_path),
        "--random_seed",
        str(ctx.seed),
        "--num_epoch",
        str(profile_epochs(ctx)),
        "--eval_steps",
        "100",
        "--log_step",
        "50",
        "--batch_size",
        "64",
    ]
    commands.append(
        run_command(
            docker_command_with_env(
                ctx,
                "tricycle/t5chem:runtime",
                train_args,
                env={"WANDB_MODE": "offline", "WANDB_DIR": ctx.container_path(fold_root / "wandb")},
                use_gpu=ctx.gpu_device is not None,
            ),
            log_path=fold_root / "logs" / "train.log",
            cwd=ctx.workspace,
            timeout_seconds=ctx.timeout_seconds,
            dry_run=ctx.dry_run,
        )
    )
    raw_predictions = fold_root / "raw_predictions.csv"
    predict_args = [
        "predict",
        "--data_dir",
        ctx.container_path(data_dir),
        "--model_dir",
        ctx.container_path(model_dir),
        "--prediction",
        ctx.container_path(raw_predictions),
        "--batch_size",
        "32",
    ]
    commands.append(
        run_command(
            docker_command(
                ctx, "tricycle/t5chem:runtime", predict_args, use_gpu=ctx.gpu_device is not None
            ),
            log_path=fold_root / "logs" / "predict.log",
            cwd=ctx.workspace,
            timeout_seconds=ctx.timeout_seconds,
            dry_run=ctx.dry_run,
        )
    )
    metrics: dict[str, Any] | None = None
    predictions = fold_root / "predictions.csv"
    if not ctx.dry_run:
        y_pred = read_first_numeric_column(raw_predictions, preferred="prediction")
        metrics = write_normalized_predictions(
            predictions, ctx.dataset, split.test, ctx.target, y_pred
        )
    return {
        "model": "t5chem",
        "fold": split.fold,
        "predictions": str(predictions),
        "metrics": metrics,
        "commands": commands,
    }
