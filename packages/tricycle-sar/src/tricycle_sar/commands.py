from __future__ import annotations

from collections.abc import Sequence

try:
    from sar_runtime.errors import InvalidRequestError  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
except ImportError:
    from tricycle_core.errors import InvalidRequestError


def _non_empty(value: object, *, name: str) -> str:
    text = "" if value is None else str(value)
    if not text.strip():
        raise InvalidRequestError(f"{name} must not be empty")
    return text


def _extra_args(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(str(value) for value in values)
    if any(not value.strip() for value in result):
        raise InvalidRequestError("RxnResid command arguments must not be empty")
    return result


def _optional_text(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    return _non_empty(str(value), name=name)


def _positive(value: int | None, *, name: str) -> int | None:
    if value is not None and value < 1:
        raise InvalidRequestError(f"{name} must be positive when provided")
    return value


def _positive_float(value: float | None, *, name: str) -> float | None:
    if value is not None and value <= 0.0:
        raise InvalidRequestError(f"{name} must be positive when provided")
    return value


def _append_option(
    command: tuple[str, ...],
    option: str,
    value: object | None,
) -> tuple[str, ...]:
    if value is None:
        return command
    return command + (option, str(value))


def build_training_command(
    *,
    python_executable: str = "python3.12",
    script: str = "train.py",
    config: str = "configs/rxnresid.yaml",
    output: str = "runs/default",
    fold_index: int | None = None,
    split_manifest: str | None = None,
    epochs: int | None = None,
    batch_size: int | None = None,
    device: str | None = None,
    mixed_precision: str | None = None,
    seed: int | None = None,
    model: str | None = None,
    mapping_mode: str | None = None,
    encoder: str | None = None,
    path_encoder: str | None = None,
    substrate_encoder: str | None = None,
    product_encoder: str | None = None,
    train_on_train_valid_from_scratch: bool = False,
    two_stage_train_valid_from_scratch: bool = False,
    selection_refit_from_scratch: bool = False,
    stage1_epochs: int | None = None,
    stage2_epochs: int | None = None,
    stage2_learning_rate_factor: float | None = None,
    stage2_auxiliary_weight_factor: float | None = None,
    extra_args: Sequence[str] = (),
) -> tuple[str, ...]:
    """Build the official RxnResid regular-training command.

    RxnResid's regular path requires ``--fold-index``.  Its checkpoint
    fine-tuning path is a different upstream protocol and is exposed by
    :func:`build_finetune_command`; it must not be mixed into this builder.
    """

    if fold_index is None:
        raise InvalidRequestError("fold_index is required for regular RxnResid training")
    if fold_index < 0:
        raise InvalidRequestError("fold_index must not be negative")
    epochs = _positive(epochs, name="epochs")
    batch_size = _positive(batch_size, name="batch_size")
    stage1_epochs = _positive(stage1_epochs, name="stage1_epochs")
    stage2_epochs = _positive(stage2_epochs, name="stage2_epochs")
    stage2_learning_rate_factor = _positive_float(
        stage2_learning_rate_factor,
        name="stage2_learning_rate_factor",
    )
    if stage2_auxiliary_weight_factor is not None and stage2_auxiliary_weight_factor < 0.0:
        raise InvalidRequestError("stage2_auxiliary_weight_factor must be non-negative")
    if train_on_train_valid_from_scratch and two_stage_train_valid_from_scratch:
        raise InvalidRequestError("from-scratch training modes are mutually exclusive")
    if selection_refit_from_scratch and (
        train_on_train_valid_from_scratch or two_stage_train_valid_from_scratch
    ):
        raise InvalidRequestError("from-scratch training modes are mutually exclusive")
    if selection_refit_from_scratch and stage1_epochs is not None:
        raise InvalidRequestError("stage1_epochs is not used by selection_refit_from_scratch")
    if two_stage_train_valid_from_scratch and (stage1_epochs is None or stage2_epochs is None):
        raise InvalidRequestError(
            "stage1_epochs and stage2_epochs are required for two-stage training"
        )
    if (
        not two_stage_train_valid_from_scratch
        and not selection_refit_from_scratch
        and (stage1_epochs is not None or stage2_epochs is not None)
    ):
        raise InvalidRequestError(
            "stage1_epochs and stage2_epochs require a from-scratch staged mode"
        )
    if selection_refit_from_scratch and stage2_epochs is None:
        raise InvalidRequestError(
            "stage2_epochs is required for selection-refit-from-scratch training"
        )
    if device is not None and device not in {"auto", "cpu"}:
        raise InvalidRequestError("device must be auto or cpu")
    if mixed_precision is not None and mixed_precision not in {"no", "fp16", "bf16"}:
        raise InvalidRequestError("mixed_precision must be no, fp16, or bf16")
    if model is not None and model != "rxnresid":
        raise InvalidRequestError("model must be rxnresid")
    if mapping_mode is not None and mapping_mode not in {
        "none",
        "edit_features",
        "latent_diff",
        "combined",
    }:
        raise InvalidRequestError(
            "mapping_mode must be none, edit_features, latent_diff, or combined"
        )

    config_value = _optional_text(config, name="config")
    split_manifest = _optional_text(split_manifest, name="split_manifest")
    output = _non_empty(output, name="output")
    command = (
        _non_empty(python_executable, name="python_executable"),
        _non_empty(script, name="script"),
    )
    if config_value is not None:
        command += ("--config", config_value)
    command += (
        "--output",
        output,
        "--fold-index",
        str(fold_index),
    )
    command = _append_option(command, "--split-manifest", split_manifest)
    command = _append_option(command, "--epochs", epochs)
    command = _append_option(command, "--batch-size", batch_size)
    command = _append_option(command, "--device", device)
    command = _append_option(command, "--mixed-precision", mixed_precision)
    command = _append_option(command, "--seed", seed)
    command = _append_option(command, "--model", model)
    command = _append_option(command, "--mapping-mode", mapping_mode)
    command = _append_option(command, "--encoder", encoder)
    command = _append_option(command, "--path-encoder", path_encoder)
    command = _append_option(command, "--substrate-encoder", substrate_encoder)
    command = _append_option(command, "--product-encoder", product_encoder)
    if train_on_train_valid_from_scratch:
        command += ("--train-on-train-valid-from-scratch",)
    if two_stage_train_valid_from_scratch:
        command += ("--two-stage-train-valid-from-scratch",)
    if selection_refit_from_scratch:
        command += ("--selection-refit-from-scratch",)
    command = _append_option(command, "--stage1-epochs", stage1_epochs)
    command = _append_option(command, "--stage2-epochs", stage2_epochs)
    command = _append_option(
        command,
        "--stage2-learning-rate-factor",
        stage2_learning_rate_factor,
    )
    command = _append_option(
        command,
        "--stage2-auxiliary-weight-factor",
        stage2_auxiliary_weight_factor,
    )
    return command + _extra_args(extra_args)


def build_finetune_command(
    *,
    checkpoint: str,
    python_executable: str = "python3.12",
    script: str = "train.py",
    output: str = "runs/refit",
    epochs: int | None = None,
    batch_size: int | None = None,
    device: str | None = None,
    mixed_precision: str | None = None,
    epoch_factor: float | None = None,
    learning_rate_factor: float | None = None,
    extra_args: Sequence[str] = (),
) -> tuple[str, ...]:
    """Build RxnResid checkpoint refit, the upstream fine-tuning protocol.

    ``train.py --refit`` loads the configuration, fold and model settings from
    the selection checkpoint, initializes a fresh optimizer, and trains on
    train+valid.  Consequently this command deliberately omits ``--config``,
    ``--fold-index``, ``--seed`` and model/encoder overrides.
    """

    checkpoint = _non_empty(checkpoint, name="checkpoint")
    epochs = _positive(epochs, name="epochs")
    batch_size = _positive(batch_size, name="batch_size")
    epoch_factor = _positive_float(epoch_factor, name="epoch_factor")
    learning_rate_factor = _positive_float(
        learning_rate_factor,
        name="learning_rate_factor",
    )
    if device is not None and device not in {"auto", "cpu"}:
        raise InvalidRequestError("device must be auto or cpu")
    if mixed_precision is not None and mixed_precision not in {"no", "fp16", "bf16"}:
        raise InvalidRequestError("mixed_precision must be no, fp16, or bf16")
    command = (
        _non_empty(python_executable, name="python_executable"),
        _non_empty(script, name="script"),
        "--refit",
        "--checkpoint",
        checkpoint,
        "--output",
        _non_empty(output, name="output"),
    )
    command = _append_option(command, "--epochs", epochs)
    command = _append_option(command, "--batch-size", batch_size)
    command = _append_option(command, "--device", device)
    command = _append_option(command, "--mixed-precision", mixed_precision)
    command = _append_option(command, "--epoch-factor", epoch_factor)
    command = _append_option(command, "--learning-rate-factor", learning_rate_factor)
    return command + _extra_args(extra_args)




def build_inference_command(
    *,
    checkpoint: str,
    python_executable: str = "python3.12",
    script: str = "predict.py",
    data: str = "data/new_full_df_exact_balanced_40ene_40diene.csv",
    output: str = "predictions.csv",
    batch_size: int | None = None,
    cpu: bool = False,
    mixed_precision: str | None = None,
    component_id_columns: Sequence[str] = (),
    extra_args: Sequence[str] = (),
) -> tuple[str, ...]:
    """Build the official RxnResid prediction command for a Bohr worker."""

    if batch_size is not None and batch_size < 1:
        raise InvalidRequestError("batch_size must be positive when provided")
    if mixed_precision is not None and mixed_precision not in {"no", "fp16", "bf16"}:
        raise InvalidRequestError("mixed_precision must be no, fp16, or bf16")
    component_id_columns = tuple(str(value) for value in component_id_columns)
    if any(not value.strip() for value in component_id_columns):
        raise InvalidRequestError("component_id_columns must not contain empty values")
    command = (
        _non_empty(python_executable, name="python_executable"),
        _non_empty(script, name="script"),
        "--checkpoint",
        _non_empty(checkpoint, name="checkpoint"),
        "--data",
        _non_empty(data, name="data"),
        "--output",
        _non_empty(output, name="output"),
    )
    if batch_size is not None:
        command += ("--batch-size", str(batch_size))
    if cpu:
        command += ("--cpu",)
    if mixed_precision is not None:
        command += ("--mixed-precision", mixed_precision)
    if component_id_columns:
        command += ("--component-id-columns", *component_id_columns)
    return command + _extra_args(extra_args)
