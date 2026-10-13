"""Common initialization and execution contracts for every registered SAR model."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from sar_runtime.toml import load_toml  # type: ignore[import-not-found]  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
except ImportError:
    from tricycle_core.toml import load_toml


def expected_accelerator(task: dict) -> str:
    """Derive initialization requirements from the environment, not a bundle name."""
    model = task.get("environment_model", task.get("model", ""))
    return "cpu" if model == "classical" or model.startswith("fingerprint_") else "cuda"


def training_command(task: dict, python: str, result: Path) -> list[str]:
    """Choose the scientific kernel while sharing the surrounding job lifecycle."""
    model, fold = task["model"], task["fold"]
    if type(fold) is not int or fold not in range(5):
        raise ValueError("Invalid SAR fold")
    smoke = bool(task.get("smoke", False))
    bundle = task["bundle_name"]
    if bundle == "classical_bundle" and model in (
        "fingerprint_rf",
        "fingerprint_extra_trees",
        "fingerprint_ridge",
    ):
        return [
            python,
            "-u",
            "scripts/tricycle_classical.py",
            "--config",
            "configs/tricycle-classical.toml",
            "--model",
            model,
            "--fold",
            str(fold),
            *(["--smoke"] if smoke else []),
        ]
    if bundle == "native_bundle" and model in ("chemprop_cgr", "chemprop_multicomponent", "t5chem"):
        return [python, "-u", "scripts/sar_native_runner.py", "--task", task["task_path"]]
    if bundle == "molecular_bundle" and model in ("uam", "molclr"):
        script = "scripts/tricycle_molecular.py"
        arguments = [
            "--config",
            f"configs/tricycle-{model}.toml",
            "--model",
            model,
            "--fold",
            str(fold),
            *(["--smoke"] if smoke else []),
        ]
    elif (
        bundle == "probe_bundle" and smoke and model in ("rxnresid", "rxngraphormer", "reactiont5")
    ):
        script = "scripts/sar_additional_probe.py"
        arguments = ["--model", model]
    elif bundle == "training_bundle" and model in ("rxnresid", "rxngraphormer", "reactiont5"):
        if smoke:
            raise ValueError("Complete training kernel requires a full fold")
        script = "scripts/sar_complete_train_runner.py"
        arguments = ["--model", model, "--fold", str(fold), "--task", task["task_path"]]
    else:
        raise ValueError("SAR model and scientific kernel differ")
    return [
        python,
        "-u",
        "scripts/sar_accelerator_exec.py",
        "--expected",
        expected_accelerator(task),
        "--report",
        str(result / f"{model}-fold-{fold}" / "accelerator.toml"),
        "--mode",
        "smoke" if smoke else "train",
        "--script",
        script,
        "--",
        *arguments,
    ]


def initializer_command(task: dict, common: Path, prefix: Path, bundle: Path) -> list[str]:
    model = task["environment_model"]
    command = [
        sys.executable,
        str(common / "sar_model_envs/engine.py"),
        "--model",
        model,
        "--root",
        str(prefix),
        "--execute",
    ]
    source = common / f"{model}.tar.gz"
    if source.is_file():
        command += ["--source-archive", str(source), "--source-sha256", task["source_sha256"]]
    if (bundle / "assets").is_dir():
        command += ["--assets-directory", str(bundle / "assets")]
    if model == "rxngraphormer":
        command += [
            "--conda-executable",
            str(common / "micromamba"),
            "--conda-sha256",
            task["conda_sha256"],
        ]
    return command


def execute(task_path: Path, common: Path, result: Path, *, initialize_only: bool = False) -> None:
    task_path, common, result = task_path.resolve(), common.resolve(), result.resolve()
    task = {**load_toml(task_path), "task_path": str(task_path)}
    task.setdefault("model", task.get("models", [None])[0])
    model = task["model"]
    task.setdefault(
        "environment_model",
        "classical"
        if task["bundle_name"] == "classical_bundle"
        else "chemprop"
        if model.startswith("chemprop_")
        else model,
    )
    try:
        from sar_runtime.io import file_sha256  # type: ignore[import-not-found]  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    except ImportError:
        from tricycle_core.io import file_sha256
    for name, digest in task.get("input_hashes", {}).items():
        path = (common / name).resolve()
        if not path.is_relative_to(common) or file_sha256(path) != digest:
            raise ValueError(f"SAR input hash mismatch: {name}")
    name = task["bundle_name"]
    if Path(name).name != name or name in (".", ".."):
        raise ValueError("Unsafe SAR bundle")
    bundle = common / name
    accelerator = expected_accelerator(task)
    environment = {
        **os.environ,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "WANDB_MODE": "offline",
    }
    if accelerator == "cpu":
        environment.update(
            CUDA_VISIBLE_DEVICES="",
            OMP_NUM_THREADS="1",
            OPENBLAS_NUM_THREADS="1",
            MKL_NUM_THREADS="1",
        )
    prefix = common.parent / ".sar-model-environment"
    result.mkdir(parents=True, exist_ok=True)

    def stage(label: str, seconds: int, command: list[str], *, progress: bool = False) -> None:
        argv = [
            sys.executable,
            str(common / "sar_stage.py"),
            "--name",
            label,
            "--output",
            str(result / "stages"),
            "--accelerator",
            accelerator,
            "--timeout",
            str(seconds),
        ]
        if progress:
            argv += [
                "--progress-timeout",
                "600",
                "--evidence",
                str(result / f"{task['model']}-fold-{task['fold']}" / "accelerator.toml"),
            ]
        subprocess.run([*argv, "--", *command], cwd=bundle, env=environment, check=True)

    try:
        if not (prefix / "ready").is_file():
            stage("initialize", 2400, initializer_command(task, common, prefix, bundle))
            (prefix / "ready").touch()
        planned = subprocess.run(
            [
                sys.executable,
                str(common / "sar_model_envs/engine.py"),
                "--model",
                task["environment_model"],
                "--root",
                str(prefix),
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        expected_directory = Path(json.loads(planned.stdout)["directory"])
        runtimes = list(prefix.glob("*/runtime.json"))
        if len(runtimes) != 1:
            raise ValueError("Expected exactly one initialized SAR environment")
        directory = runtimes[0].parent
        if directory != expected_directory:
            raise ValueError("SAR initialized environment recipe differs")
        state = load_toml(directory / "state.toml")
        if (
            state["model"] != task["environment_model"]
            or state["status"] != "environment-installed"
        ):
            raise ValueError("SAR initialized environment model or state differs")
        if initialize_only:
            return
        environment.update(json.loads(runtimes[0].read_text()))
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, (str(common), environment.get("PYTHONPATH")))
        )
        python = directory / "env/bin/python"
        if not python.is_file():
            raise ValueError("SAR model interpreter is missing")
        for model in task.get("models", [task["model"]]):
            selected = {**task, "model": model}
            stage(
                f"{model}-fold-{task['fold']}",
                7200 if accelerator == "cpu" else 86400,
                training_command(selected, str(python), result),
                progress=name in ("molecular_bundle", "probe_bundle"),
            )
    finally:
        for directory in prefix.glob("*"):
            if directory.is_dir():
                for file in (
                    "state.toml",
                    "install.log",
                    "pip-check.txt",
                    "pip-freeze.txt",
                    "environment-smoke.toml",
                ):
                    source = directory / file
                    if source.is_file():
                        shutil.copy2(source, result / f"{directory.name}-{file}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--common", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--initialize-only", action="store_true")
    args = parser.parse_args()
    execute(args.task, args.common, args.result, initialize_only=args.initialize_only)


if __name__ == "__main__":
    main()
