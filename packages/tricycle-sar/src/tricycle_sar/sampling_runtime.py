"""Stage and verify the shared RxnResid backend for sequential sampling workers."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

from tricycle_core.toml import load_toml
from tricycle_core.training_protocol import file_sha256, protocol_file_source

from .assets import load_model_asset
from .backends import get_backend
from .backends.registry import CATALOG

PACKAGES = (
    "tricycle-core",
    "tricycle-sampling",
    "tricycle-sar",
    "tricycle-bohr",
    "tricycle-bohrsubmit",
)
_ENVIRONMENT_FILES = (
    "common/rxnresid.tar.gz",
    "common/sar_model_envs/recipes.toml",
    "common/sar_model_envs/verify.py",
    "common/sar_model_envs/engine.py",
    *(
        f"common/sar_runtime/{name}.py"
        for name in ("io", "toml", "errors", "metrics", "training_protocol")
    ),
)
_API_FILES = (
    "commands.py",
    "rxnresid.py",
    "predictions.py",
    "sampling_runtime.py",
    "single_job.py",
    "backends/__init__.py",
)


def binding() -> dict[str, Any]:
    """Resolve verified package assets; neither install nor contact a service."""
    backend = get_backend("rxnresid")
    asset = load_model_asset()
    entries = {item["path"]: item for item in backend.method["files"]}
    recipe = load_toml(protocol_file_source(CATALOG, entries["common/sar_model_envs/recipes.toml"]))
    if recipe["models"]["rxnresid"]["revision"] != asset.revision:
        raise ValueError("SAR model metadata differs from the verified environment recipe")
    package = Path(__file__).parent
    return {
        "schema": "tricycle.sar-sampling-runtime/v1",
        "model": "rxnresid",
        "method_id": backend.method["method_id"],
        "source_revision": asset.revision,
        "environment": backend.environment,
        "environment_files": {
            path.removeprefix("common/"): entries[path]["sha256"] for path in _ENVIRONMENT_FILES
        },
        "api_files": {name: file_sha256(package / name) for name in _API_FILES},
    }


def validate_source_spec(spec: dict[str, Any] | None) -> None:
    """Explicit overrides may only name the same pinned, clean source."""
    if not spec:
        return
    asset = load_model_asset()
    if spec.get("revision", asset.revision) != asset.revision:
        raise ValueError("Sampling RxnResid revision must match the shared SAR backend")
    if spec.get("repository", asset.repository).removesuffix(".git") != asset.repository:
        raise ValueError("Sampling RxnResid repository must match the shared SAR backend")
    if spec.get("source_directory"):
        source = Path(spec["source_directory"]).resolve()
        from tricycle_bohr.model_source import validate_source_checkout

        validate_source_checkout(source, asset.revision)


def stage(
    root: Path, spec: dict[str, Any] | None = None, *, packages: tuple[str, ...] = PACKAGES
) -> dict[str, Any]:
    """Deliver package code and the existing environment/source assets once."""
    validate_source_spec(spec)
    record = binding()
    root.mkdir(parents=True, exist_ok=True)
    for name in packages:
        module = name.replace("-", "_")
        module_spec = importlib.util.find_spec(module)
        if module_spec is None or module_spec.origin is None:
            raise ValueError(f"Required SAR runtime package is unavailable: {name}")
        shutil.copytree(
            Path(module_spec.origin).parent,
            root / name / "src" / module,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    backend = get_backend("rxnresid")
    entries = {item["path"]: item for item in backend.method["files"]}
    runtime = root / "sar-runtime"
    for path in _ENVIRONMENT_FILES:
        target = runtime / path.removeprefix("common/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(protocol_file_source(CATALOG, entries[path]), target)
    source = root / "rxnresid"
    source.mkdir(exist_ok=True)
    with tarfile.open(runtime / "rxnresid.tar.gz") as archive:
        for member in archive.getmembers():
            target = (source / member.name).resolve()
            if not target.is_relative_to(source.resolve()) or not (
                member.isfile() or member.isdir()
            ):
                raise ValueError("Unsafe SAR source archive entry")
        archive.extractall(source, filter="data")
    if (source / ".tricycle-source-revision").read_text().strip() != record["source_revision"]:
        raise ValueError("SAR source archive revision mismatch")
    (runtime / "binding.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    verify(root, record)
    return record


def verify(root: Path, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reject drift before initialization and execution; never refresh hashes."""
    runtime = root / "sar-runtime"
    record = json.loads((runtime / "binding.json").read_text())
    if record != binding() or (expected is not None and record != expected):
        raise ValueError("Sampling SAR backend binding differs")
    for name, digest in record["environment_files"].items():
        if file_sha256(runtime / name) != digest:
            raise ValueError(f"Sampling SAR environment asset changed: {name}")
    package = root / "tricycle-sar/src/tricycle_sar"
    for name, digest in record["api_files"].items():
        if file_sha256(package / name) != digest:
            raise ValueError(f"Sampling SAR API changed: {name}")
    # Compare staged model files against the verified archive, including extras.
    source = root / "rxnresid"
    names = set()
    with tarfile.open(runtime / "rxnresid.tar.gz") as archive:
        for member in archive.getmembers():
            if member.isfile():
                names.add(member.name)
                stream = archive.extractfile(member)
                if stream is None or hashlib.sha256(stream.read()).hexdigest() != file_sha256(
                    source / member.name
                ):
                    raise ValueError(f"Sampling RxnResid source changed: {member.name}")
    actual = {
        str(p.relative_to(source))
        for p in source.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    if actual != names:
        raise ValueError("Sampling RxnResid source contains undeclared files")
    return record


def initialize(root: Path) -> None:
    """Invoke the shared SAR initializer with its verified source archive."""
    record = verify(root)
    runtime = root / "sar-runtime"
    subprocess.run(
        [
            sys.executable,
            str(runtime / "sar_model_envs/engine.py"),
            "--model",
            "rxnresid",
            "--root",
            str(root / ".sar-env"),
            "--execute",
            "--source-archive",
            str(runtime / "rxnresid.tar.gz"),
            "--source-sha256",
            record["environment_files"]["rxnresid.tar.gz"],
        ],
        check=True,
    )


def worker_launcher(*, module: str | None = None, script: str = "runner.py") -> str:
    """Use the same image-inheriting initializer for either sampling controller."""
    entrypoint = "-m " + shlex.quote(module) if module else shlex.quote(script)
    paths = ":".join(f"$PWD/{name}/src" for name in PACKAGES)
    return f"""#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy PYTHONHOME
export NO_PROXY='*' no_proxy='*' PYTHONNOUSERSITE=1 PYTHONUNBUFFERED=1
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2 TOKENIZERS_PARALLELISM=false
export PYTHONPATH="$PWD/rxnresid/src:{paths}"
python3.12 -m tricycle_sar.sampling_runtime initialize --root "$PWD"
activate="$(python3.12 -c 'from pathlib import Path; p=list(Path(".sar-env").glob("*/activate.sh")); assert len(p)==1; print(p[0].resolve())')"
source "$activate"
model_python="$(dirname "$activate")/env/bin/python"
export PYTHONPATH="$PWD/rxnresid/src:{paths}:${{PYTHONPATH:-}}"
exec "$model_python" -u {entrypoint} --request "$1"
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "initialize"))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "initialize":
        initialize(root)
    else:
        verify(root)


if __name__ == "__main__":
    main()
