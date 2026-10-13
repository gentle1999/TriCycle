"""Reusable, smoke-validated environment and training/prediction backends."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tricycle_core.training_protocol import resolve_training_registry, training_method

from .registry import CATALOG


@dataclass(frozen=True)
class ModelBackend:
    name: str
    method: dict

    @property
    def environment(self) -> dict:
        return {
            key: self.method[key]
            for key in ("environment", "executor", "image", "machine_type", "environment_mode")
        }

    def prepare(
        self,
        dataset: Path,
        splits: Path,
        output: Path,
        *,
        fold: int,
        assets: Path | None = None,
        retain_checkpoint: bool = False,
    ) -> dict:
        """Prepare the validated environment and train/predict script; no execution."""
        from tricycle_sar.training_inputs import prepare_inputs

        return prepare_inputs(
            dataset,
            splits,
            CATALOG,
            self.name,
            fold,
            output,
            method_id=self.method["method_id"],
            assets=assets,
            retain_checkpoint=retain_checkpoint,
        )

    def _native_command(self, mode: str, **options: Any) -> tuple[str, ...]:
        """Use the public SAR builders and optional frozen route vocabulary."""
        if self.name != "rxnresid":
            raise ValueError(
                f"{self.name} predicts through its validated training/evaluation runner"
            )
        from tricycle_sar.assets import load_model_asset
        from tricycle_sar.commands import (
            build_finetune_command,
            build_inference_command,
            build_training_command,
        )
        from tricycle_sar.rxnresid import route_command

        source = options.pop("source_directory", None)
        vocabulary = options.pop("route_vocabulary", None)
        asset = load_model_asset()
        entrypoint = asset.inference_entrypoint if mode == "infer" else asset.training_entrypoint
        options.setdefault("script", str(Path(source) / entrypoint) if source else entrypoint)
        if mode == "train" and not any(
            options.get(name)
            for name in (
                "train_on_train_valid_from_scratch",
                "two_stage_train_valid_from_scratch",
                "selection_refit_from_scratch",
            )
        ):
            _bind_cumulative_training_mode(options)
        builders: dict[str, Callable[..., tuple[str, ...]]] = {
            "train": build_training_command,
            "refit": build_finetune_command,
            "infer": build_inference_command,
        }
        builder = builders[mode]
        return route_command(
            builder(**options), source_directory=source, route_vocabulary=vocabulary
        )

    def training_command(self, **options: Any) -> tuple[str, ...]:
        return self._native_command("train", **options)

    def refit_command(self, **options: Any) -> tuple[str, ...]:
        return self._native_command("refit", **options)

    def inference_command(self, **options: Any) -> tuple[str, ...]:
        return self._native_command("infer", **options)


def get_backend(model: str) -> ModelBackend:
    manifest, registry = resolve_training_registry(CATALOG)
    if model not in registry["models"]:
        raise ValueError(f"Unknown SAR model: {model}")
    identifier = registry["models"][model]["default_method"]
    return ModelBackend(
        model, {"method_id": identifier, **training_method(manifest, registry, model, identifier)}
    )


def list_backends() -> tuple[str, ...]:
    _, registry = resolve_training_registry(CATALOG)
    return tuple(registry["models"])


def _bind_cumulative_training_mode(options: dict[str, Any], *, cwd: Path = Path(".")) -> None:
    """A cumulative pool has no validation split for upstream selection/refit."""
    config = options.get("config")
    if not config or not (cwd / config).is_file():
        return
    import json

    import yaml  # pyright: ignore[reportMissingImports, reportMissingModuleSource]

    settings = yaml.safe_load((cwd / config).read_text())
    if not isinstance(settings, dict):
        raise ValueError("RxnResid configuration must be a mapping")
    split = options.get("split_manifest") or settings.get("data", {}).get("split_manifest")
    if not split:
        return
    manifest = json.loads((cwd / split).read_text())
    if manifest.get("strategy") != "active_learning_all_selected_train":
        return
    folds = manifest["folds"]
    if "fold_index" not in options:
        return
    selected = next((fold for fold in folds if fold["fold_index"] == options["fold_index"]), None)
    if selected is None:
        raise ValueError("Cumulative training fold is absent from its split manifest")
    if selected["valid_group_ids"] or selected["test_group_ids"] or not selected["train_group_ids"]:
        raise ValueError("Cumulative training requires all revealed groups in train")
    options["train_on_train_valid_from_scratch"] = True


def bind_native_training_mode(command: tuple[str, ...], *, cwd: Path) -> tuple[str, ...]:
    """Apply cumulative-split semantics to an already built native command."""
    if "--refit" in command or any(
        flag in command
        for flag in (
            "--train-on-train-valid-from-scratch",
            "--two-stage-train-valid-from-scratch",
            "--selection-refit-from-scratch",
        )
    ):
        return command
    options: dict[str, Any] = {}
    for flag, name in (
        ("--config", "config"),
        ("--split-manifest", "split_manifest"),
        ("--fold-index", "fold_index"),
    ):
        if flag in command:
            index = command.index(flag)
            if index + 1 == len(command):
                raise ValueError(f"Missing value for {flag}")
            options[name] = int(command[index + 1]) if name == "fold_index" else command[index + 1]
    _bind_cumulative_training_mode(options, cwd=cwd)
    return (
        (*command, "--train-on-train-valid-from-scratch")
        if options.get("train_on_train_valid_from_scratch")
        else command
    )


__all__ = ["CATALOG", "ModelBackend", "get_backend", "list_backends"]
