from __future__ import annotations

from collections.abc import Sequence

from tricycle_core.errors import InvalidRequestError


def _non_empty(value: object, *, name: str) -> str:
    text = "" if value is None else str(value)
    if not text.strip():
        raise InvalidRequestError(f"{name} must not be empty")
    return text


def _extra_args(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(str(value) for value in values)
    if any(not value.strip() for value in result):
        raise InvalidRequestError("RitS command arguments must not be empty")
    return result


def _worker_prefix(project_dir: str) -> tuple[str, ...]:
    # Bohr executes the command from the uploaded input directory.  Keep the
    # historical argument for API compatibility, but do not invent a launcher
    # that is not part of the Bohr worker image.
    _non_empty(project_dir, name="project_dir")
    return ()


def build_training_command(
    *,
    project_dir: str = "/workspace/job",
    python_executable: str = "python3.12",
    script: str = "scripts/train.py",
    config_path: str = "conf",
    config_name: str = "rits",
    finetune_from: str | None = None,
    init_from_ckpt: str | None = None,
    resume: str | None = None,
    overrides: Sequence[str] = (),
) -> tuple[str, ...]:
    """Build the official RitS Hydra training command for a Bohr worker.

    RitS uses Hydra overrides rather than dedicated argparse switches.  The
    upstream fork defines ``finetune_from`` (with ``init_from_ckpt`` as an
    alias) for loading weights into a fresh optimizer, and ``resume`` for
    restoring the complete Lightning training state.  These modes are
    mutually exclusive in the upstream entrypoint and are kept explicit here.
    """

    if finetune_from is not None:
        finetune_from = _non_empty(str(finetune_from), name="finetune_from")
    if init_from_ckpt is not None:
        init_from_ckpt = _non_empty(str(init_from_ckpt), name="init_from_ckpt")
    if resume is not None:
        resume = _non_empty(str(resume), name="resume")
    if finetune_from is not None and init_from_ckpt is not None:
        raise InvalidRequestError("finetune_from and init_from_ckpt are mutually exclusive")
    if resume is not None and (finetune_from is not None or init_from_ckpt is not None):
        raise InvalidRequestError("resume and fine-tuning overrides are mutually exclusive")

    mode_overrides: tuple[str, ...] = ()
    if finetune_from is not None:
        mode_overrides = (f"finetune_from={finetune_from}",)
    elif init_from_ckpt is not None:
        mode_overrides = (f"init_from_ckpt={init_from_ckpt}",)
    elif resume is not None:
        mode_overrides = (f"resume={resume}",)

    return _worker_prefix(project_dir) + (
        _non_empty(python_executable, name="python_executable"),
        _non_empty(script, name="script"),
        f"--config-path={_non_empty(config_path, name='config_path')}",
        f"--config-name={_non_empty(config_name, name='config_name')}",
        *mode_overrides,
        *_extra_args(overrides),
    )


def build_finetune_command(
    *,
    checkpoint: str,
    project_dir: str = "/workspace/job",
    python_executable: str = "python3.12",
    script: str = "scripts/train.py",
    config_path: str = "conf",
    config_name: str = "rits",
    overrides: Sequence[str] = (),
) -> tuple[str, ...]:
    """Build RitS fine-tuning using ``finetune_from``.

    The checkpoint supplies model weights only.  The upstream script creates
    a fresh optimizer and scheduler, so this is intentionally distinct from
    :func:`build_resume_command`.
    """

    checkpoint = _non_empty(checkpoint, name="checkpoint")
    return build_training_command(
        project_dir=project_dir,
        python_executable=python_executable,
        script=script,
        config_path=config_path,
        config_name=config_name,
        finetune_from=checkpoint,
        overrides=overrides,
    )


def build_resume_command(
    *,
    checkpoint: str,
    project_dir: str = "/workspace/job",
    python_executable: str = "python3.12",
    script: str = "scripts/train.py",
    config_path: str = "conf",
    config_name: str = "rits",
    overrides: Sequence[str] = (),
) -> tuple[str, ...]:
    """Build RitS interrupted-run continuation using ``resume``."""

    checkpoint = _non_empty(checkpoint, name="checkpoint")
    return build_training_command(
        project_dir=project_dir,
        python_executable=python_executable,
        script=script,
        config_path=config_path,
        config_name=config_name,
        resume=checkpoint,
        overrides=overrides,
    )


def build_inference_command(
    *,
    project_dir: str = "/workspace/job",
    python_executable: str = "python3.12",
    script: str = "scripts/sample_transition_state.py",
    config: str = "scripts/conf/rits.yaml",
    checkpoint: str = "data/rits.ckpt",
    output: str = "outputs/rits.xyz",
    reactant_smi: str | None = None,
    product_smi: str | None = None,
    reaction_smarts: str | None = None,
    reaction_file: str | None = None,
    reactant_xyz: str | None = None,
    product_xyz: str | None = None,
    n_samples: int = 1,
    batch_size: int = 32,
    charge: int = 0,
    num_steps: int = 16,
    kekulize: bool = False,
    add_stereo: bool = False,
    device: str = "auto",
    extra_args: Sequence[str] = (),
) -> tuple[str, ...]:
    """Build a RitS transition-state sampling command for a Bohr worker."""

    if n_samples < 1 or batch_size < 1 or num_steps < 1:
        raise InvalidRequestError("n_samples, batch_size, and num_steps must be positive")
    smi_pair = (reactant_smi is not None, product_smi is not None)
    xyz_pair = (reactant_xyz is not None, product_xyz is not None)
    if smi_pair == (True, False) or smi_pair == (False, True):
        raise InvalidRequestError("reactant_smi and product_smi must be provided together")
    if xyz_pair == (True, False) or xyz_pair == (False, True):
        raise InvalidRequestError("reactant_xyz and product_xyz must be provided together")

    smarts_values = tuple(
        (name, value)
        for name, value in (
            ("reaction_smarts", reaction_smarts),
            ("reaction_file", reaction_file),
        )
        if value is not None
    )
    if len(smarts_values) > 1:
        raise InvalidRequestError("provide either reaction_smarts or reaction_file, not both")
    input_modes = (
        int(smi_pair == (True, True)) + int(xyz_pair == (True, True)) + int(bool(smarts_values))
    )
    if input_modes > 1:
        raise InvalidRequestError(
            "provide one RitS input mode: SMILES, XYZ, reaction SMARTS, or file"
        )
    if input_modes == 0:
        raise InvalidRequestError(
            "provide a SMILES pair, an XYZ pair, reaction_smarts, or reaction_file"
        )

    input_args: tuple[str, ...]
    if smi_pair == (True, True):
        input_args = (
            "--reactant_smi",
            _non_empty(str(reactant_smi), name="reactant_smi"),
            "--product_smi",
            _non_empty(str(product_smi), name="product_smi"),
        )
    elif xyz_pair == (True, True):
        input_args = (
            "--reactant_xyz",
            _non_empty(str(reactant_xyz), name="reactant_xyz"),
            "--product_xyz",
            _non_empty(str(product_xyz), name="product_xyz"),
        )
    elif smarts_values:
        name, value = smarts_values[0]
        input_args = (f"--{name}", _non_empty(str(value), name=name))
    else:
        raise InvalidRequestError("no valid RitS input mode")
    return _worker_prefix(project_dir) + (
        _non_empty(python_executable, name="python_executable"),
        _non_empty(script, name="script"),
        *input_args,
        "--config",
        _non_empty(config, name="config"),
        "--ckpt",
        _non_empty(checkpoint, name="checkpoint"),
        "--output",
        _non_empty(output, name="output"),
        "--n_samples",
        str(n_samples),
        "--batch_size",
        str(batch_size),
        "--charge",
        str(charge),
        "--num_steps",
        str(num_steps),
        "--device",
        _non_empty(device, name="device"),
        *(("--kekulize",) if kekulize else ()),
        *(("--add_stereo",) if add_stereo else ()),
        *_extra_args(extra_args),
    )


# Keep both spellings available to callers that use a prose-derived name.
build_fine_tuning_command = build_finetune_command
