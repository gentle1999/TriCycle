from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
from scripts.tricycle_model_adapters.workflow_common import REACTION_COLUMN, FoldSplit, WorkflowContext, docker_command, profile_epochs, profile_num_workers, profile_warmup_epochs, read_first_numeric_column, run_command, write_normalized_predictions  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
from sklearn.metrics import mean_absolute_error, mean_squared_error  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip


@dataclass(frozen=True)
class ChempropParams:
    name: str
    message_hidden_dim: int
    depth: int
    dropout: float
    ffn_hidden_dim: int
    ffn_num_layers: int

    def to_manifest(self) -> dict[str, int | float | str]:
        return {
            "name": self.name,
            "message_hidden_dim": self.message_hidden_dim,
            "depth": self.depth,
            "dropout": self.dropout,
            "ffn_hidden_dim": self.ffn_hidden_dim,
            "ffn_num_layers": self.ffn_num_layers,
        }


@dataclass(frozen=True)
class ChempropMode:
    model_name: str
    input_columns: tuple[str, ...]
    input_args: tuple[str, ...]
    description: str

    def to_manifest(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "input_columns": list(self.input_columns),
            "input_args": list(self.input_args),
            "description": self.description,
        }


CHEMPROP_CGR_MODE = ChempropMode(
    model_name="chemprop_cgr",
    input_columns=(REACTION_COLUMN,),
    input_args=("--reaction-columns", REACTION_COLUMN, "--rxn-mode", "REAC_DIFF"),
    description="Chemprop reaction/CGR mode using REAC_DIFF reaction featurization.",
)
CHEMPROP_MULTICOMPONENT_MODE = ChempropMode(
    model_name="chemprop_multicomponent",
    input_columns=("reactants", "products"),
    input_args=("--smiles-columns", "reactants", "products"),
    description="Chemprop multicomponent mode with separate reactant and product molecular inputs.",
)
CHEMPROP_GROUP_MODELS = ("chemprop_cgr", "chemprop_multicomponent")
CHEMPROP_MODES = {
    CHEMPROP_CGR_MODE.model_name: CHEMPROP_CGR_MODE,
    CHEMPROP_MULTICOMPONENT_MODE.model_name: CHEMPROP_MULTICOMPONENT_MODE,
}


def split_reaction_smiles(rxn_smiles: str) -> tuple[str, str, str]:
    parts = rxn_smiles.split(">")
    if len(parts) != 3:
        from scripts.tricycle_model_adapters.workflow_common import WorkflowRunError  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

        raise WorkflowRunError(f"Invalid reaction SMILES: {rxn_smiles}")
    return parts[0], parts[1], parts[2]


def write_chemprop_csv(
    path: Path, df: pd.DataFrame, indices: list[int], target: str, mode: ChempropMode
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode.model_name == CHEMPROP_CGR_MODE.model_name:
        df.iloc[indices][[REACTION_COLUMN, target]].to_csv(path, index=False)
        return

    rows: list[dict[str, Any]] = []
    for _, row in df.iloc[indices].iterrows():
        reactants, agents, products = split_reaction_smiles(str(row[REACTION_COLUMN]))
        reactant_component = ".".join(part for part in (reactants, agents) if part)
        rows.append(
            {
                "reactants": reactant_component,
                "products": products,
                target: row[target],
            }
        )
    pd.DataFrame(rows, columns=[*mode.input_columns, target]).to_csv(path, index=False)


def chemprop_train_args_for_params(
    ctx: WorkflowContext,
    train_csv: Path,
    val_csv: Path,
    test_csv: Path,
    output_dir: Path,
    params: ChempropParams,
    mode: ChempropMode,
) -> list[str]:
    return standard_train_args(
        ctx,
        train_csv,
        val_csv,
        test_csv,
        output_dir,
        mode=mode,
        message_hidden_dim=params.message_hidden_dim,
        depth=params.depth,
        dropout=params.dropout,
        ffn_hidden_dim=params.ffn_hidden_dim,
        ffn_num_layers=params.ffn_num_layers,
    )


def standard_train_args(
    ctx: WorkflowContext,
    train_csv: Path,
    val_csv: Path,
    test_csv: Path,
    output_dir: Path,
    *,
    mode: ChempropMode,
    message_hidden_dim: int = 128,
    depth: int = 3,
    dropout: float = 0.0,
    ffn_hidden_dim: int = 128,
    ffn_num_layers: int = 2,
) -> list[str]:
    return [
        "train",
        "--data-path",
        ctx.container_path(train_csv),
        ctx.container_path(val_csv),
        ctx.container_path(test_csv),
        *mode.input_args,
        "--target-columns",
        ctx.target,
        "--task-type",
        "regression",
        "--epochs",
        str(profile_epochs(ctx)),
        "--warmup-epochs",
        str(profile_warmup_epochs(ctx)),
        "--batch-size",
        "64",
        "--num-workers",
        str(profile_num_workers(ctx)),
        "--accelerator",
        "gpu" if ctx.gpu_device is not None else "cpu",
        "--devices",
        "1",
        "--data-seed",
        str(ctx.seed),
        "--pytorch-seed",
        str(ctx.seed),
        "--multi-hot-atom-featurizer-mode",
        "V2",
        "--aggregation",
        "norm",
        "--aggregation-norm",
        "100",
        "--activation",
        "RELU",
        "--message-hidden-dim",
        str(message_hidden_dim),
        "--depth",
        str(depth),
        "--dropout",
        str(dropout),
        "--ffn-hidden-dim",
        str(ffn_hidden_dim),
        "--ffn-num-layers",
        str(ffn_num_layers),
        "--init-lr",
        "0.0001",
        "--max-lr",
        "0.001",
        "--final-lr",
        "0.0001",
        "--output-dir",
        ctx.container_path(output_dir),
    ]


CHEMPROP_QUICK_HPO_CANDIDATES = [
    ChempropParams(
        "small", message_hidden_dim=64, depth=2, dropout=0.0, ffn_hidden_dim=64, ffn_num_layers=1
    ),
    ChempropParams(
        "standard",
        message_hidden_dim=128,
        depth=3,
        dropout=0.1,
        ffn_hidden_dim=128,
        ffn_num_layers=2,
    ),
]


CHEMPROP_STANDARD_HPO_CANDIDATES = [
    ChempropParams(
        "standard_300",
        message_hidden_dim=300,
        depth=3,
        dropout=0.0,
        ffn_hidden_dim=300,
        ffn_num_layers=1,
    ),
    ChempropParams(
        "standard_300_dropout",
        message_hidden_dim=300,
        depth=3,
        dropout=0.1,
        ffn_hidden_dim=300,
        ffn_num_layers=1,
    ),
    ChempropParams(
        "wide_600",
        message_hidden_dim=600,
        depth=3,
        dropout=0.0,
        ffn_hidden_dim=600,
        ffn_num_layers=1,
    ),
]


def chemprop_hpo_candidates(profile: str) -> list[ChempropParams]:
    if profile == "quick":
        return CHEMPROP_QUICK_HPO_CANDIDATES
    return CHEMPROP_STANDARD_HPO_CANDIDATES


def run_chemprop_outer_hpo(
    ctx: WorkflowContext,
    fold_root: Path,
    train_csv: Path,
    val_csv: Path,
    test_csv: Path,
    mode: ChempropMode,
) -> tuple[ChempropParams, list[dict[str, Any]]]:
    hpo_root = fold_root / "hpo"
    commands: list[dict[str, Any]] = []
    trials: list[dict[str, Any]] = []
    candidates = chemprop_hpo_candidates(ctx.profile)
    for trial_index, candidate in enumerate(candidates):
        trial_root = hpo_root / f"trial_{trial_index}_{candidate.name}"
        commands.append(
            run_command(
                docker_command(
                    ctx,
                    "tricycle/chemprop:runtime",
                    chemprop_train_args_for_params(
                        ctx, train_csv, val_csv, test_csv, trial_root / "train", candidate, mode
                    ),
                    use_gpu=ctx.gpu_device is not None,
                ),
                log_path=trial_root / "logs" / "train.log",
                cwd=ctx.workspace,
                timeout_seconds=ctx.timeout_seconds,
                dry_run=ctx.dry_run,
            )
        )
        raw_val_predictions = trial_root / "val_raw_predictions.csv"
        commands.append(
            run_command(
                docker_command(
                    ctx,
                    "tricycle/chemprop:runtime",
                    [
                        "predict",
                        "--test-path",
                        ctx.container_path(val_csv),
                        *mode.input_args,
                        "--model-paths",
                        ctx.container_path(trial_root / "train"),
                        "--output",
                        ctx.container_path(raw_val_predictions),
                        "--accelerator",
                        "gpu" if ctx.gpu_device is not None else "cpu",
                        "--devices",
                        "1",
                        "--num-workers",
                        "0",
                    ],
                    use_gpu=ctx.gpu_device is not None,
                ),
                log_path=trial_root / "logs" / "predict_val.log",
                cwd=ctx.workspace,
                timeout_seconds=ctx.timeout_seconds,
                dry_run=ctx.dry_run,
            )
        )
        metric: dict[str, Any] | None = None
        if not ctx.dry_run:
            y_pred = read_first_numeric_column(raw_val_predictions, preferred=ctx.target)
            val_indices = pd.read_csv(val_csv).index.tolist()
            # val_csv has been materialized in fold order, so compute directly against its target column.
            val_df = pd.read_csv(val_csv)
            mse = mean_squared_error(val_df[ctx.target].astype(float), y_pred)
            metric = {
                "rmse": float(mse**0.5),
                "mae": float(mean_absolute_error(val_df[ctx.target].astype(float), y_pred)),
                "rows": len(val_indices),
            }
        trials.append({"candidate": candidate.to_manifest(), "validation": metric})

    if ctx.dry_run:
        selected = candidates[0]
    else:
        selected_index = min(
            range(len(trials)),
            key=lambda index: float(trials[index]["validation"]["rmse"]),
        )
        selected = candidates[selected_index]
    selection = {
        "method": "outer_grid_search",
        "selection_metric": "validation_rmse",
        "selected": selected.to_manifest(),
        "trials": trials,
    }
    hpo_root.mkdir(parents=True, exist_ok=True)
    (hpo_root / "selection.json").write_text(
        json.dumps(selection, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return selected, commands


def run_chemprop_fold(
    ctx: WorkflowContext, split: FoldSplit, *, hpopt: bool, mode: ChempropMode
) -> dict[str, Any]:
    fold_root = ctx.output / mode.model_name / f"fold_{split.fold}"
    data_dir = fold_root / "data"
    train_csv = data_dir / "train.csv"
    val_csv = data_dir / "val.csv"
    test_csv = data_dir / "test.csv"
    write_chemprop_csv(train_csv, ctx.dataset, split.train, ctx.target, mode)
    write_chemprop_csv(val_csv, ctx.dataset, split.val, ctx.target, mode)
    write_chemprop_csv(test_csv, ctx.dataset, split.test, ctx.target, mode)

    commands: list[dict[str, Any]] = []
    selected_params = chemprop_hpo_candidates(ctx.profile)[0]
    if hpopt:
        selected_params, hpo_commands = run_chemprop_outer_hpo(
            ctx, fold_root, train_csv, val_csv, test_csv, mode
        )
        commands.extend(hpo_commands)

    train_dir = fold_root / "train"
    commands.append(
        run_command(
            docker_command(
                ctx,
                "tricycle/chemprop:runtime",
                chemprop_train_args_for_params(
                    ctx, train_csv, val_csv, test_csv, train_dir, selected_params, mode
                ),
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
        "--test-path",
        ctx.container_path(test_csv),
        *mode.input_args,
        "--model-paths",
        ctx.container_path(train_dir),
        "--output",
        ctx.container_path(raw_predictions),
        "--accelerator",
        "gpu" if ctx.gpu_device is not None else "cpu",
        "--devices",
        "1",
        "--num-workers",
        "0",
    ]
    commands.append(
        run_command(
            docker_command(
                ctx, "tricycle/chemprop:runtime", predict_args, use_gpu=ctx.gpu_device is not None
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
        y_pred = read_first_numeric_column(raw_predictions, preferred=ctx.target)
        metrics = write_normalized_predictions(
            predictions, ctx.dataset, split.test, ctx.target, y_pred
        )
    return {
        "model": mode.model_name,
        "fold": split.fold,
        "predictions": str(predictions),
        "metrics": metrics,
        "hyperparameters": selected_params.to_manifest(),
        "chemprop_mode": mode.to_manifest(),
        "commands": commands,
    }
