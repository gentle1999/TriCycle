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


def build_training_command(
    *,
    python_executable: str = "python3.12",
    script: str = "scripts/train.py",
    config_path: str = "conf",
    config_name: str = "rits",
    finetune_from: str | None = None,
    resume: str | None = None,
    overrides: Sequence[str] = (),
) -> tuple[str, ...]:
    """Build the official RitS Hydra training command for a Bohr worker.

    RitS uses Hydra overrides rather than dedicated argparse switches.  The
    upstream fork defines ``finetune_from`` for loading weights into a fresh
    optimizer, and ``resume`` for
    restoring the complete Lightning training state.  These modes are
    mutually exclusive in the upstream entrypoint and are kept explicit here.
    """

    if finetune_from is not None:
        finetune_from = _non_empty(str(finetune_from), name="finetune_from")
    if resume is not None:
        resume = _non_empty(str(resume), name="resume")
    if resume is not None and finetune_from is not None:
        raise InvalidRequestError("resume and fine-tuning overrides are mutually exclusive")

    mode_overrides: tuple[str, ...] = ()
    if finetune_from is not None:
        mode_overrides = (f"finetune_from={finetune_from}",)
    elif resume is not None:
        mode_overrides = (f"resume={resume}",)

    return (
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
    python_executable: str = "python3.12",
    script: str = "scripts/train.py",
    config_path: str = "conf",
    config_name: str = "rits",
    overrides: Sequence[str] = (),
) -> tuple[str, ...]:
    """Build RitS interrupted-run continuation using ``resume``."""

    checkpoint = _non_empty(checkpoint, name="checkpoint")
    return build_training_command(
        python_executable=python_executable,
        script=script,
        config_path=config_path,
        config_name=config_name,
        resume=checkpoint,
        overrides=overrides,
    )


def build_inference_command(
    *,
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
    return (
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


def build_ts_pipeline_command(
    *,
    python_executable: str = "python3.12",
    script: str = "scripts/run_ts_pipeline.py",
    reactions: str = "inputs/reaction.smiles",
    config: str = "scripts/conf/rits.yaml",
    checkpoint: str = "data/rits.ckpt",
    candidates: str = "outputs/rits-candidates",
    output: str = "outputs/rits-pipeline",
    generate: bool = True,
    generation_strategy: str = "hybrid",
    n_samples: int = 100,
    num_steps: int = 16,
    device: str = "cuda:0",
    batch_size: int = 100,
    max_atoms_per_batch: int | None = 18000,
    cluster_threshold: float = 0.5,
    cluster_atoms: str = "heavy",
    stereo_filter: bool = True,
    energy_scope: str = "staged",
    energy_window_hartree: float = 0.01,
    gxtb_binary: str | None = None,
    energy_workers: int = 1,
    omp_threads: int = 1,
    energy_timeout: float = 300.0,
    charge: int | None = None,
    uhf: int | None = None,
    extra_args: Sequence[str] = (),
) -> tuple[str, ...]:
    """Build the RitS candidate-generation, clustering, and g-xTB command.

    This mirrors the configured fork's ``scripts/run_ts_pipeline.py``.  The
    reaction input is a file because the pipeline's ``--reactions``
    interface accepts a newline-delimited reaction file or CSV, not an inline
    SMILES argument.  A caller that owns the worker staging area must create
    that file before executing the command.
    """

    if device == "auto":
        raise InvalidRequestError(
            "run_ts_pipeline.py generation does not support device=auto; use cuda:0 or cpu"
        )
    if generation_strategy not in {"hybrid", "global"}:
        raise InvalidRequestError("generation_strategy must be hybrid or global")
    if cluster_atoms not in {"heavy", "all"}:
        raise InvalidRequestError("cluster_atoms must be heavy or all")
    if energy_scope not in {"staged", "medoids", "all"}:
        raise InvalidRequestError("energy_scope must be staged, medoids, or all")
    if n_samples < 1 or num_steps < 1 or batch_size < 1:
        raise InvalidRequestError("pipeline n_samples, num_steps, and batch_size must be positive")
    if max_atoms_per_batch is not None and max_atoms_per_batch < 1:
        raise InvalidRequestError("max_atoms_per_batch must be positive when provided")
    if cluster_threshold < 0.0 or energy_window_hartree < 0.0:
        raise InvalidRequestError("pipeline thresholds must be non-negative")
    if energy_workers < 1 or omp_threads < 1 or energy_timeout <= 0.0:
        raise InvalidRequestError("pipeline energy settings must be positive")

    command = [
        _non_empty(python_executable, name="python_executable"),
        _non_empty(script, name="script"),
        "--reactions",
        _non_empty(reactions, name="reactions"),
        "--config",
        _non_empty(config, name="config"),
        "--ckpt",
        _non_empty(checkpoint, name="checkpoint"),
        "--candidates",
        _non_empty(candidates, name="candidates"),
        "--output",
        _non_empty(output, name="output"),
    ]
    if generate:
        command.append("--generate")
    command.extend(
        (
            "--generation-strategy",
            generation_strategy,
            "--n-samples",
            str(n_samples),
            "--num-steps",
            str(num_steps),
            "--device",
            _non_empty(device, name="device"),
        )
    )
    if max_atoms_per_batch is not None:
        command.extend(("--max-atoms-per-batch", str(max_atoms_per_batch)))
    else:
        command.extend(("--batch-size", str(batch_size)))
    command.extend(
        (
            "--cluster-threshold",
            str(cluster_threshold),
            "--cluster-atoms",
            cluster_atoms,
            "--energy-scope",
            energy_scope,
            "--energy-window-hartree",
            str(energy_window_hartree),
            "--energy-workers",
            str(energy_workers),
            "--omp-threads",
            str(omp_threads),
            "--energy-timeout",
            str(energy_timeout),
        )
    )
    if stereo_filter:
        command.append("--stereo-filter")
    else:
        command.append("--no-stereo-filter")
    if gxtb_binary is not None:
        command.extend(("--gxtb-binary", _non_empty(gxtb_binary, name="gxtb_binary")))
    if charge is not None:
        command.extend(("--charge", str(charge)))
    if uhf is not None:
        command.extend(("--uhf", str(uhf)))
    command.extend(_extra_args(extra_args))
    return tuple(command)
