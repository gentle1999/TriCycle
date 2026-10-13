"""Run pinned registry fold adapters directly in a prepared Bohr environment."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

try:
    from sar_runtime.io import file_sha256 as core_file_sha256  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
except ImportError:
    from tricycle_core.io import file_sha256 as core_file_sha256


def direct_command(ctx, image, args, *, env=None, use_gpu=False):
    """Translate only known registry container entrypoints to installed executables."""
    entries = {"tricycle/chemprop:runtime": ["chemprop"], "tricycle/t5chem:runtime": ["t5chem"]}
    if image not in entries:
        raise ValueError(f"Unsupported native entrypoint: {image}")
    translated = [
        str(ctx.workspace / arg.removeprefix("/workspace/"))
        if arg.startswith("/workspace/")
        else arg
        for arg in args
    ]
    training = args[0] == "train"
    identity = hashlib.sha256(json.dumps([image, args]).encode()).hexdigest()[:16]
    evidence = ctx.output / "accelerator" / f"{identity}.toml"
    mode = "train" if training else "predict"
    guarded = [
        sys.executable,
        "-u",
        str(ctx.workspace / "scripts/sar_accelerator_exec.py"),
        "--expected",
        "cuda",
        "--report",
        str(evidence),
        "--mode",
        mode,
        "--entrypoint",
        entries[image][0],
        "--",
        *translated,
    ]
    stage = [
        sys.executable,
        str(ctx.workspace / "scripts/sar_stage.py"),
        "--name",
        identity,
        "--output",
        str(ctx.output / "stages"),
        "--accelerator",
        "cuda",
        "--timeout",
        str(ctx.timeout_seconds),
    ]
    if training:
        stage += ["--evidence", str(evidence), "--progress-timeout", "600"]
    return [
        "env",
        *(f"{key}={value}" for key, value in (env or {}).items()),
        *stage,
        "--",
        *guarded,
    ]


def live_command(command, *, log_path, cwd, timeout_seconds, dry_run, env=None):
    """Keep the registry log file and also stream child output into the worker log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return {"command": command, "log": str(log_path), "returncode": 0, "skipped": True}
    with log_path.open("x") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if process.stdout is None:
            raise RuntimeError("Model process stdout is unavailable")
        try:
            for line in process.stdout:
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
            code = process.wait(timeout=5)
        finally:
            process.stdout.close()
        if code:
            raise RuntimeError(f"Model command exited {code}: {log_path}")
    return {"command": command, "log": str(log_path), "returncode": code, "skipped": False}


def validate_task(task, root: Path):
    """Check serialized task and frozen files before loading any model runtime."""
    if task["model"] not in {"chemprop_cgr", "chemprop_multicomponent", "t5chem"}:
        raise ValueError("Unsupported model")
    if type(task["fold"]) is not int or task["fold"] not in range(5):
        raise ValueError("Invalid fold")
    expected_env = "chemprop" if task["model"].startswith("chemprop_") else "t5chem"
    if task["environment_model"] != expected_env:
        raise ValueError("Wrong model environment")
    if not task["hashes"]:
        raise ValueError("Missing input hashes")
    seen = set()
    for entry in task["hashes"]:
        name, digest = entry["path"], entry["sha256"]
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or name in seen:
            raise ValueError("Invalid input path")
        seen.add(name)
        if core_file_sha256(root / relative) != digest:
            raise ValueError(f"Input checksum mismatch: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, required=True)
    args = parser.parse_args()
    task = tomllib.loads(args.task.read_text())
    root = Path.cwd()
    validate_task(task, root)
    sys.path.insert(0, str(root))
    import pandas as pd  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    import torch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from scripts.tricycle_model_adapters.workflow_common import FoldSplit, WorkflowContext  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    if not torch.cuda.is_available():
        raise RuntimeError("GPU is required")
    dataset = pd.read_csv(root / "data/snapshots/ground-truth/dataset.csv")
    if "row_index" not in dataset:
        dataset["row_index"] = range(len(dataset))
    if dataset["row_index"].tolist() != list(range(len(dataset))):
        raise ValueError("Unexpected source row indices")
    indices = {str(value): index for index, value in enumerate(dataset.reaction_id)}
    roles = {"train": [], "val": [], "test": []}
    with (root / "data/splits/ground-truth-5fold/assignments.csv").open() as handle:
        for row in csv.DictReader(handle):
            if int(row["fold"]) == task["fold"]:
                roles[row["split"]].append(indices[row["row_id"]])
    if sorted(sum(roles.values(), [])) != list(range(len(dataset))):
        raise ValueError("Fold does not cover dataset exactly once")
    groups = [
        set(zip(dataset.iloc[rows].ene_id, dataset.iloc[rows].diene_id, strict=True))
        for rows in roles.values()
    ]
    if any(groups[i] & groups[j] for i in range(3) for j in range(i)):
        raise ValueError("Group leakage")
    model = task["model"]
    module_name = "chemprop" if model.startswith("chemprop_") else model
    module: Any = importlib.import_module(
        f"scripts.tricycle_model_adapters.workflow_adapters.{module_name}"
    )
    # Explicit process-boundary replacement; upstream data/model/training logic is retained.
    module.run_command = live_command
    module.docker_command = direct_command
    module.docker_command_with_env = direct_command
    output = root / ("smoke-results" if task.get("smoke") else "model-runs") / model
    fold_output = output / model / f"fold_{task['fold']}"
    if fold_output.exists():
        raise FileExistsError(fold_output)
    ctx = WorkflowContext(
        root,
        root / "data/snapshots/ground-truth",
        root / "data/splits/ground-truth-5fold/manifest.json",
        dataset,
        output,
        "G_T_activate",
        42,
        86400,
        "0",
        False,
        "quick" if task.get("smoke") else "standard",
        False,
    )
    split = FoldSplit(task["fold"], **roles)
    if module_name == "chemprop":
        result = module.run_chemprop_fold(
            ctx, split, hpopt=False, mode=module.CHEMPROP_MODES[model]
        )
    elif module_name == "t5chem":
        result = module.run_t5chem_fold(ctx, split)
    else:
        raise ValueError(model)
    (fold_output / "native-runtime.json").write_text(
        json.dumps(
            {
                "task": task,
                "result": result,
                "device": torch.cuda.get_device_name(0),
                "torch": str(torch.__version__),
                "python": sys.version,
                "executable": sys.executable,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
