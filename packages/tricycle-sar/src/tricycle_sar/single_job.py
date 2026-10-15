"""Bind one RxnResid task to the same source and runtime as sampling workers."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from tricycle_bohr import BohrModelJobRequest
from tricycle_core import ExecutionContext, ModelMode
from tricycle_core.errors import InvalidRequestError
from tricycle_core.io import file_sha256
from tricycle_core.toml import dump_toml, load_toml
from tricycle_core.training_protocol import protocol_file_source

from .backends import CATALOG, bind_native_training_mode, get_backend
from .delivery import freeze
from .sampling_runtime import binding, stage, verify, worker_launcher


def input_hashes(root: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(root.rglob("*")):
        if {".git", ".venv", "__pycache__"}.intersection(path.relative_to(root).parts):
            continue
        if path.is_symlink():
            raise InvalidRequestError(f"SAR inputs must not contain symlinks: {path}")
        if path.is_file():
            hashes[path.relative_to(root).as_posix()] = file_sha256(path)
    return hashes


def validate_source(source: Path) -> None:
    backend = get_backend("rxnresid")
    entry = next(e for e in backend.method["files"] if e["path"] == "common/rxnresid.tar.gz")
    with tarfile.open(protocol_file_source(CATALOG, entry)) as archive:
        for member in archive.getmembers():
            if not member.isfile() or member.name == ".tricycle-source-revision":
                continue
            stream = archive.extractfile(member)
            path = source / member.name
            if stream is None or not path.is_file() or path.read_bytes() != stream.read():
                raise InvalidRequestError(
                    f"RxnResid source differs from the shared backend: {member.name}"
                )


def bind_request(request: BohrModelJobRequest, context: ExecutionContext) -> dict:
    backend = binding()
    if request.image != backend["environment"]["image"]:
        raise InvalidRequestError("RxnResid image must match the shared SAR backend")
    if request.machine_type and request.machine_type != backend["environment"]["machine_type"]:
        raise InvalidRequestError("RxnResid machine_type must match the shared SAR backend")
    command = request.command
    entrypoint = "predict.py" if request.mode is ModelMode.INFER else "train.py"
    if (
        len(command) < 2
        or not Path(command[0]).name.startswith("python")
        or Path(command[1]).name != entrypoint
    ):
        raise InvalidRequestError(
            "Use the shared RxnResid command builder; arbitrary launchers are not supported"
        )
    if request.mode is ModelMode.FINETUNE and "--refit" not in command:
        raise InvalidRequestError("RxnResid finetune requires the shared checkpoint refit command")
    if request.mode is ModelMode.TRAIN and ("--refit" in command or "--fold-index" not in command):
        raise InvalidRequestError(
            "RxnResid training requires a fold and cannot use checkpoint refit"
        )
    if "--output" not in command or command.index("--output") == len(command) - 1:
        raise InvalidRequestError("SAR requires an explicit result output path")
    output = Path(command[command.index("--output") + 1])
    if output.is_absolute() or ".." in output.parts or not output.parts or output.parts[0] in {
        "job-inputs", "job-work", "rxnresid", "sar-runtime", "worker.log", "task.toml", "worker.sh"
    } or output.parts[0].startswith("tricycle-"):
        raise InvalidRequestError("SAR result path must be separate from uploaded inputs/runtime")
    root = (context.workdir / str(request.input_directory)).resolve()
    assert request.code_artifact is not None
    source = (context.workdir / request.code_artifact.location).resolve()
    if not source.is_relative_to(root):
        raise InvalidRequestError("RxnResid code_artifact must be inside input_directory")
    hashes = {}
    if root.exists():
        validate_source(source)
        hashes = input_hashes(root)
    elif not context.dry_run:
        raise InvalidRequestError("Local RxnResid task inputs are required before submission")
    return {
        "backend": backend,
        "input_directory": str(root),
        "source_directory": str(source),
        "input_hashes": hashes,
        "command": list(command),
        "mode": request.mode.value,
        "result_path": output.as_posix(),
    }


def prepare(record: dict, output: Path) -> Path:
    """Revalidate the planned inputs and stage a shared worker without executing it."""
    root = Path(record["input_directory"])
    if record["backend"] != binding():
        raise InvalidRequestError("Planned SAR backend binding differs")
    if not record["input_hashes"] or input_hashes(root) != record["input_hashes"]:
        raise InvalidRequestError("SAR task inputs changed or were unavailable during planning")
    validate_source(Path(record["source_directory"]))
    if output.exists():
        raise FileExistsError(output)
    stage(
        output, packages=("tricycle-core", "tricycle-sar", "tricycle-bohr", "tricycle-bohrsubmit")
    )
    inputs = output / "job-inputs"
    for name, digest in record["input_hashes"].items():
        target = inputs / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
        if file_sha256(target) != digest:
            raise InvalidRequestError(f"SAR input changed while staging: {name}")
    # Host absolute paths inside the uploaded directory become relative worker paths.
    command = []
    for value in record["command"]:
        path = Path(value)
        if path.is_absolute() and path.is_relative_to(root):
            value = path.relative_to(root).as_posix()
        command.append(value)
    dump_toml(output / "task.toml", {**record, "command": command})
    (output / "worker.sh").write_text(worker_launcher(module="tricycle_sar.single_job"))
    (output / "worker.sh").chmod(0o755)
    freeze(output, tuple(output.iterdir()))
    return output


def run(request: Path) -> None:
    root = request.resolve().parent
    task = load_toml(request)
    verify(root, task["backend"])
    inputs = root / "job-inputs"
    if input_hashes(inputs) != task["input_hashes"]:
        raise ValueError("Uploaded SAR task inputs differ")
    command = list(task["command"])
    if task["mode"] == "train":
        command = list(bind_native_training_mode(tuple(command), cwd=inputs))
    command[:2] = [sys.executable, str(root / "rxnresid" / Path(command[1]).name)]
    index = command.index("--output") + 1
    destination = (root / command[index]).resolve()
    if not destination.is_relative_to(root) or destination == root:
        raise ValueError("SAR outputs must remain in the worker directory")
    work = root / "job-work" / ("predictions.csv" if task["mode"] == "infer" else "training")
    work.parent.mkdir(parents=True, exist_ok=True)
    command[index] = str(work)
    try:
        with (root / "worker.log").open("a") as log:
            subprocess.run(command, cwd=inputs, stdout=log, stderr=subprocess.STDOUT, check=True)
    finally:
        if task["mode"] == "infer":
            if work.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(work, destination)
        else:
            # Only native generated results; uploaded/refit weights and preprocessed data stay local.
            for name in (
                "checkpoint.pt", "history.json", "stage_histories.json", "training_summary.json",
                "training_protocol.json", "refit_history.json", "refit_summary.json",
                "test_metrics.json", "test_predictions.csv", "selection_predictions.csv",
                "resolved_run.json", "split.json", "preprocessing_report.json",
                "preprocessing_anomalies.csv",
            ):
                path = work / name
                if path.is_file():
                    destination.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination / name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    run(parser.parse_args().request)


if __name__ == "__main__":
    main()
