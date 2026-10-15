"""Prepare pinned RxnResid/ReactionT5 five-fold Bohr batches; never submit."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from tricycle_core.asset_store import resolve_input
from tricycle_core.toml import load_toml
from tricycle_core.training_protocol import verify_asset_files

from tricycle_sar.delivery import freeze
from tricycle_sar.modelling_benchmark import check
from tricycle_sar.submission import bundle_hashes, write_bohr_request
from tricycle_sar.training import (
    digest,
    protocol,
    protocol_file,
    require_benchmark_protocol,
    stage_protocol_bundle,
)


def require_reactiont5_assets(assets: Path | None, spec: dict) -> None:
    """Require the pinned checkpoint locally before staging any remote inputs."""
    if assets is None:
        raise ValueError(
            "complete local ReactionT5 assets are required; worker HF downloads are disabled"
        )
    if not any(
        entry["path"] == "pytorch_model.bin" and "sha256" in entry for entry in spec["files"]
    ):
        raise ValueError("ReactionT5 assets must declare the pinned checkpoint SHA256")
    missing = verify_asset_files(assets, spec["files"])
    if missing:
        raise ValueError(
            f"incomplete local ReactionT5 assets: {missing}; prepare them before submission"
        )


def prepare(
    model: str,
    benchmark: Path,
    output: Path,
    *,
    assets: Path | None = None,
    group_name: str,
    project_id: str | None = None,
    group_config: Path | None = None,
) -> Path:
    if model not in {"rxnresid", "rxngraphormer", "reactiont5"}:
        raise ValueError(f"unsupported additional model: {model}")
    record, _ = protocol(model)
    if record["executor"] != "bohr":
        raise ValueError(f"{model} requires its pinned internal executor")
    if not project_id and group_config is None:
        raise ValueError("SAR preparation requires explicit group_config or project_id")
    check(benchmark)
    require_benchmark_protocol(benchmark)
    archive = protocol_file(model, f"common/{model}.tar.gz")
    source_hash = digest(archive)
    if model == "reactiont5":
        spec = json.loads(
            protocol_file(model, "common/training_bundle/reactiont5.json").read_text()
        )
        require_reactiont5_assets(assets, spec)
    output.mkdir(parents=True, exist_ok=False)
    bundle = output / "training_bundle"
    shutil.copytree(resolve_input(benchmark, "workspace/data"), bundle / "data", copy_function=shutil.copyfile)
    splits = bundle / "data/splits/ground-truth-5fold/assignments.csv"
    splits.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(resolve_input(benchmark, "splits.csv"), splits)
    prefix = f"common/{bundle.name}/assets/"
    for entry in record["files"]:
        if entry["external"] and entry["path"].startswith(prefix):
            destination = bundle / "assets" / entry["path"].removeprefix(prefix)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(assets / entry["path"].removeprefix(prefix), destination)
    stage_protocol_bundle(model, bundle)
    manifest = {
        "dataset_sha256": load_toml(benchmark / "run.toml")["dataset_sha256"],
        "splits_sha256": digest(resolve_input(benchmark, "splits.csv")),
        "files": bundle_hashes(bundle),
    }
    (bundle / "training-bundle.json").write_text(json.dumps(manifest, indent=2) + "\n")
    write_bohr_request(
        model,
        output,
        bundle,
        task_defaults={
            "model": model,
            "smoke": False,
            "source_sha256": source_hash,
            "benchmark_dataset_sha256": manifest["dataset_sha256"],
            "benchmark_splits_sha256": manifest["splits_sha256"],
        },
        task_specs=[{"name": f"fold-{fold}", "fold": fold} for fold in range(5)],
        workers=5,
        job_prefix=f"gt5-{model}",
        max_run_time="1440",
        filename="bohr-train.toml",
        group_name=group_name,
        project_id=project_id,
        group_config=group_config,
    )
    freeze(output, (bundle, output / "protocol-runtime"))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", choices=["rxnresid", "rxngraphormer", "reactiont5"], required=True
    )
    parser.add_argument("--benchmark-run", type=Path, required=True)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--job-group-name", required=True)
    parser.add_argument("--group-config", type=Path, required=True)
    args = parser.parse_args()
    prepare(
        args.model,
        args.benchmark_run,
        args.out_dir,
        assets=args.assets,
        group_name=args.job_group_name,
        group_config=args.group_config,
    )


if __name__ == "__main__":
    main()
