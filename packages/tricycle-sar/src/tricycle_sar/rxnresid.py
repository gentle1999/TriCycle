"""Shared native RxnResid data, checkpoint and route-vocabulary adapters.

The caller supplies its task's data/splits and training budget. Model architecture,
losses and optimizer defaults remain those of the pinned upstream source.
"""

from __future__ import annotations

import argparse
import copy
import csv
import functools
import json
import runpy
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

try:
    from sar_runtime.io import file_sha256 as core_file_sha256  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
except ImportError:
    from tricycle_core.io import file_sha256 as core_file_sha256


def load_route_vocabulary(path: Path) -> dict[str, int]:
    vocabulary = json.loads(path.read_text())["route_id_vocabulary"]
    if not isinstance(vocabulary, dict) or not vocabulary:
        raise ValueError("route vocabulary must be a nonempty mapping")
    if any(not isinstance(k, str) or type(v) is not int for k, v in vocabulary.items()):
        raise ValueError("route labels must be strings and indices must be integers")
    if sorted(vocabulary.values()) != list(range(len(vocabulary))):
        raise ValueError("route vocabulary indices must be unique and contiguous")
    return vocabulary


def freeze_route_vocabulary(prior: Path, output: Path) -> None:
    with prior.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or any(float(row["G_T_activate"]) != 0 for row in rows):
        raise ValueError("route vocabulary requires nonempty label-free structures")
    labels = sorted({str(row["prod_id"]).strip() for row in rows})
    if not all(labels):
        raise ValueError("empty structural route label")
    output.write_text(
        json.dumps(
            {
                "schema": "tricycle.rxnresid-route-vocabulary/v1",
                "policy": "frozen from label-free prior structures before training",
                "source_sha256": core_file_sha256(prior),
                "route_id_vocabulary": {label: index for index, label in enumerate(labels)},
            },
            indent=2,
        )
        + "\n"
    )


def run_native(source: Path, entrypoint: str, vocabulary_path: Path, argv: list[str]) -> None:
    vocabulary = load_route_vocabulary(vocabulary_path)
    sys.path[:0] = [str(source / "src"), str(source)]
    from rxnresid.data.dataset import ReactionGroupDataset  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    original = ReactionGroupDataset.__init__

    @functools.wraps(original)
    def with_vocabulary(self: Any, *args: Any, **kwargs: Any) -> None:
        provided = kwargs.get("route_id_vocabulary")
        if provided is not None and provided != vocabulary:
            raise ValueError(
                "checkpoint route vocabulary differs from frozen structural vocabulary"
            )
        kwargs["route_id_vocabulary"] = vocabulary
        original(self, *args, **kwargs)

    saved_argv = sys.argv
    ReactionGroupDataset.__init__ = with_vocabulary
    try:
        sys.argv = [str(source / entrypoint), *argv]
        runpy.run_path(str(source / entrypoint), run_name="__main__")
    finally:
        ReactionGroupDataset.__init__ = original
        sys.argv = saved_argv


def load_base_config(source_directory: Path) -> dict[str, Any]:
    try:
        import yaml  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    except ImportError as exc:
        raise RuntimeError("PyYAML is required by the RxnResid worker image") from exc
    config_path = source_directory / "configs" / "rxnresid.yaml"
    if not config_path.is_file():
        raise ValueError(f"RxnResid base config not found: {config_path}")
    value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"RxnResid config is not a mapping: {config_path}")
    return value


def write_round_config(
    path: Path,
    base_config: Mapping[str, Any],
    *,
    training_csv: Path,
    split_manifest: Path,
    output_dir: Path,
    epochs: int,
    batch_size_paths: int,
    device: str,
    mixed_precision: str,
    seed: int,
) -> None:
    import yaml  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    config = copy.deepcopy(dict(base_config))
    data = config.setdefault("data", {})
    training = config.setdefault("training", {})
    if not isinstance(data, dict) or not isinstance(training, dict):
        raise ValueError("RxnResid base config data/training sections must be mappings")
    data["path"] = str(training_csv)
    data["split_manifest"] = str(split_manifest)
    data["cache_enabled"] = False
    data["cache_dir"] = str(output_dir / "dataset-cache")
    data["target_column"] = "G_T_activate"
    data["reaction_column"] = "rxn_smiles"
    data["path_id_column"] = "path_id"
    data["include_stereo"] = True
    data["baseline_reduction"] = "mean"
    data["component_id_columns"] = ["ene_id", "diene_id"]
    data["route_id_column"] = "prod_id"
    training["epochs"] = epochs
    training["batch_size_paths"] = batch_size_paths
    training["device"] = device
    training["mixed_precision"] = mixed_precision
    training["seed"] = seed
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def write_all_train_manifest(
    training_csv: Path, manifest_path: Path, *, route_vocabulary: Path | None = None
) -> int:
    from rxnresid.data.dataset import ReactionGroupDataset  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from rxnresid.data.split import FoldAssignment, SplitManifest, dataset_group_fingerprint, dataset_record_fingerprint, save_split_manifest  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    vocabulary_kwargs = {}
    if route_vocabulary is not None:
        vocabulary_kwargs["route_id_vocabulary"] = load_route_vocabulary(route_vocabulary)
    dataset = ReactionGroupDataset(
        training_csv,
        target_column="G_T_activate",
        reaction_column="rxn_smiles",
        path_id_column="path_id",
        include_stereo=True,
        route_id_column="prod_id",
        baseline_reduction="mean",
        cache_dir=None,
        **vocabulary_kwargs,
    )
    manifest = SplitManifest(
        schema_version=1,
        strategy="active_learning_all_selected_train",
        seed=0,
        num_folds=1,
        dataset_group_count=len(dataset),
        dataset_fingerprint=dataset_group_fingerprint(dataset),
        dataset_row_count=len(dataset.rows),
        dataset_record_fingerprint=dataset_record_fingerprint(dataset),
        folds=(
            FoldAssignment(
                fold_index=0,
                train_group_ids=tuple(dataset.group_ids),
                valid_group_ids=(),
                test_group_ids=(),
            ),
        ),
    )
    save_split_manifest(manifest, manifest_path)
    return len(dataset.rows)


def prepare_refit_checkpoint(
    previous_checkpoint: Path,
    refit_checkpoint: Path,
    *,
    training_csv: Path,
    split_manifest: Path,
    fixed_seed: int,
) -> None:
    """Point a copy of the previous weights at the current cumulative dataset."""

    import torch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    checkpoint = torch.load(
        previous_checkpoint,
        map_location="cpu",
        weights_only=False,
    )
    if not isinstance(checkpoint, dict):
        raise ValueError(f"invalid RxnResid checkpoint: {previous_checkpoint}")
    raw_config = checkpoint.get("config")
    if not isinstance(raw_config, Mapping):
        raise ValueError(f"checkpoint has no project config: {previous_checkpoint}")
    config = copy.deepcopy(dict(raw_config))
    raw_data = config.setdefault("data", {})
    if not isinstance(raw_data, dict):
        raise ValueError(f"checkpoint data config is invalid: {previous_checkpoint}")
    raw_data["path"] = str(training_csv.resolve())
    raw_data["split_manifest"] = str(split_manifest.resolve())
    raw_data["cache_enabled"] = False
    checkpoint["config"] = config

    # Refit recomputes normalization statistics for the expanded labeled pool.
    checkpoint["target_statistics"] = None
    raw_resolved = checkpoint.get("resolved_run")
    if isinstance(raw_resolved, Mapping):
        resolved = copy.deepcopy(dict(raw_resolved))
        resolved["seed"] = fixed_seed
        checkpoint["resolved_run"] = resolved

    refit_checkpoint.parent.mkdir(parents=True, exist_ok=True)
    torch.save(checkpoint, refit_checkpoint)


def route_command(
    command: Sequence[str],
    *,
    source_directory: Path | None = None,
    route_vocabulary: Path | None = None,
) -> tuple[str, ...]:
    """Apply the same frozen vocabulary to native train/refit/predict invocations."""
    command = tuple(command)
    if route_vocabulary is None:
        return command
    load_route_vocabulary(route_vocabulary)
    if source_directory is None or Path(command[1]).name not in {"train.py", "predict.py"}:
        raise ValueError("route vocabulary requires a native RxnResid source and entrypoint")
    return (
        command[0],
        "-m",
        "tricycle_sar.rxnresid",
        "--source-directory",
        str(source_directory),
        "--entrypoint",
        Path(command[1]).name,
        "--route-vocabulary",
        str(route_vocabulary),
        "--",
        *command[2:],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-directory", type=Path, required=True)
    parser.add_argument("--entrypoint", choices=("train.py", "predict.py"), required=True)
    parser.add_argument("--route-vocabulary", type=Path, required=True)
    parser.add_argument("native_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    native = args.native_args[1:] if args.native_args[:1] == ["--"] else args.native_args
    run_native(args.source_directory.resolve(), args.entrypoint, args.route_vocabulary, native)


def fit_all_revealed(
    data: Path,
    out: Path,
    recipe: Mapping[str, Any],
    *,
    source: Path,
    previous: Path | None = None,
    route_vocabulary: Path | None = None,
) -> Path:
    """Train/refit cumulative native labels with the shared backend and config contract."""
    import subprocess

    from tricycle_sar.backends import get_backend

    out.mkdir(parents=True, exist_ok=True)
    split = out / "split.json"
    write_all_train_manifest(data, split, route_vocabulary=route_vocabulary)
    config = out / "config.yaml"
    write_round_config(
        config,
        load_base_config(source),
        training_csv=data,
        split_manifest=split,
        output_dir=out,
        epochs=recipe["epochs"],
        batch_size_paths=recipe["batch_size_paths"],
        device=recipe["device"],
        mixed_precision=recipe.get("mixed_precision", "no"),
        seed=recipe.get("seed", 42),
    )
    backend = get_backend("rxnresid")
    options = {
        "python_executable": sys.executable,
        "source_directory": source,
        "route_vocabulary": route_vocabulary,
        "output": str(out / "model"),
        "epochs": recipe["epochs"],
        "batch_size": recipe["batch_size_paths"],
        "device": recipe["device"],
        "mixed_precision": recipe.get("mixed_precision", "no"),
    }
    if previous:
        refit = out / "refit-input.pt"
        prepare_refit_checkpoint(
            previous,
            refit,
            training_csv=data,
            split_manifest=split,
            fixed_seed=recipe.get("seed", 42),
        )
        command = backend.refit_command(checkpoint=str(refit), **options)
    else:
        command = backend.training_command(
            config=str(config), fold_index=0, seed=recipe.get("seed", 42), **options
        )
    with (out / "train.log").open("w") as log:
        subprocess.run(command, cwd=source, stdout=log, stderr=subprocess.STDOUT, check=True)
    checkpoint = out / "model/checkpoint.pt"
    if not checkpoint.is_file():
        raise ValueError("SAR training produced no checkpoint")
    return checkpoint


if __name__ == "__main__":
    main()
