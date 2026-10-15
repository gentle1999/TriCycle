"""Prepare portable CPU smoke/training requests; never submit jobs."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from tricycle_core.asset_store import resolve_input

from tricycle_sar.classical_runner import MODELS
from tricycle_sar.delivery import freeze
from tricycle_sar.modelling_benchmark import check, run_models
from tricycle_sar.submission import write_bohr_request
from tricycle_sar.training import require_benchmark_protocol, stage_protocol_bundle


def prepare(
    run: Path, output: Path, *, include_smoke: bool = False, group_config: Path
) -> None:
    check(run)
    require_benchmark_protocol(run)
    models = [model for model in run_models(run) if model in MODELS]
    if not models:
        raise ValueError("Benchmark has no classical models")
    output.mkdir(parents=True, exist_ok=False)
    bundle = output / "classical_bundle"
    for relative in (
        "data/snapshots/ground-truth/dataset.csv",
        "data/splits/ground-truth-5fold/assignments.csv",
    ):
        destination = bundle / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolve_input(run, "workspace") / relative, destination)
    stage_protocol_bundle(models[0], bundle)
    for mode in ("smoke", "train") if include_smoke else ("train",):
        rows = (
            [{"name": "smoke", "models": models, "fold": 0}]
            if mode == "smoke"
            else [
                {"name": f"{model}-{fold}", "models": [model], "fold": fold}
                for model in models
                for fold in range(5)
            ]
        )
        write_bohr_request(
            models[0],
            output,
            bundle,
            task_defaults={"smoke": mode == "smoke"},
            task_specs=rows,
            workers=1 if mode == "smoke" else 3,
            job_prefix=f"sar-classical-{mode}",
            max_run_time="30" if mode == "smoke" else "120",
            filename=f"bohr-{mode}.toml",
            group_config=group_config,
        )

    freeze(output, (bundle, output / "protocol-runtime"))

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
