"""Prepare UAM/MolCLR Bohr requests from frozen inputs; never submit."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from tricycle_core.asset_store import resolve_input
from tricycle_core.toml import load_toml

from tricycle_sar.delivery import freeze
from tricycle_sar.modelling_benchmark import check, run_models
from tricycle_sar.submission import write_bohr_request
from tricycle_sar.training import (
    digest,
    protocol,
    protocol_file,
    require_benchmark_protocol,
    stage_protocol_bundle,
)


def prepare(
    run: Path, output: Path, *, include_smoke: bool = False, group_config: Path
) -> None:
    check(run)
    require_benchmark_protocol(run)
    models = [model for model in run_models(run) if model in ("uam", "molclr")]
    if not models:
        raise ValueError("Benchmark has no molecular models")
    # Validate every required external asset before creating any model output.
    external: dict[str, list[tuple[Path, str]]] = {}
    for model in models:
        record, _ = protocol(model)
        prefix = f"common/{record['bundle_name']}/"
        external[model] = []
        for entry in record["files"]:
            if entry["external"]:
                relative = entry["path"].removeprefix(prefix)
                source = resolve_input(run, "workspace") / relative
                if not source.is_file() or digest(source) != entry["sha256"]:
                    raise ValueError(f"missing or changed {model} asset: {relative}")
                external[model].append((source, relative))
    output.mkdir(parents=True, exist_ok=False)
    for model in models:
        record, _ = protocol(model)
        config = load_toml(
            protocol_file(model, f"common/{record['bundle_name']}/{record['config_template']}")
        )
        directory = output / model
        bundle = directory / "molecular_bundle"
        for relative in (config["dataset"], config["splits"]):
            destination = bundle / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(resolve_input(run, "workspace") / relative, destination)
        for source, relative in external[model]:
            destination = bundle / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        stage_protocol_bundle(model, bundle)
        for mode in ("smoke", "train") if include_smoke else ("train",):
            rows = [
                {"name": f"{mode}-{fold}", "fold": fold}
                for fold in ([0] if mode == "smoke" else range(5))
            ]
            write_bohr_request(
                model,
                directory,
                bundle,
                task_defaults={"model": model, "smoke": mode == "smoke"},
                task_specs=rows,
                workers=1 if mode == "smoke" else 5,
                job_prefix=f"sar-{model}-{mode}",
                max_run_time="30" if mode == "smoke" else "1440",
                filename=f"bohr-{mode}.toml",
                group_config=group_config,
            )

        freeze(directory, (bundle, directory / "protocol-runtime"))

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-run", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--include-smoke", action="store_true")
    parser.add_argument("--group-config", required=True, type=Path)
    args = parser.parse_args()
    prepare(
        args.benchmark_run, args.out_dir,
        include_smoke=args.include_smoke, group_config=args.group_config,
    )


if __name__ == "__main__":
    main()
