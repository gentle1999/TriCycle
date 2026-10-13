"""Prepare direct-runtime Chemprop and T5Chem jobs without remote effects."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from tricycle_sar.backends.native_runner import validate_task
from tricycle_sar.modelling_benchmark import check, run_models
from tricycle_sar.submission import bundle_hashes, write_bohr_request
from tricycle_sar.training import (
    digest,
    protocol_file,
    require_benchmark_protocol,
    stage_protocol_bundle,
)


def prepare(
    run: Path, output: Path, *,
    include_smoke: bool = False, group_config: Path,
) -> None:
    check(run)
    require_benchmark_protocol(run)
    models = [
        m for m in run_models(run) if m in ("chemprop_cgr", "chemprop_multicomponent", "t5chem")
    ]
    if not models:
        raise ValueError("Benchmark has no native models")
    for model in models:
        environment = "chemprop" if model.startswith("chemprop_") else model
        root = output / model
        root.mkdir(parents=True, exist_ok=False)
        bundle = root / "native_bundle"
        shutil.copytree(run / "workspace/data", bundle / "data")
        stage_protocol_bundle(model, bundle)
        hashes = bundle_hashes(bundle)
        source_hash = digest(protocol_file(model, f"common/{environment}.tar.gz"))
        for mode in ("smoke", "train") if include_smoke else ("train",):
            defaults = {
                "model": model,
                "environment_model": environment,
                "smoke": mode == "smoke",
                "hashes": hashes,
                "source_sha256": source_hash,
            }
            rows = [
                {"name": f"{mode}-{fold}", "fold": fold}
                for fold in ([0] if mode == "smoke" else range(5))
            ]
            for row in rows:
                validate_task({**defaults, "fold": row["fold"]}, bundle)
            write_bohr_request(
                model,
                root,
                bundle,
                task_defaults=defaults,
                task_specs=rows,
                workers=1 if mode == "smoke" else 5,
                job_prefix=f"gt5-{model}-{mode}",
                max_run_time="60" if mode == "smoke" else "1440",
                filename=f"bohr-{mode}.toml",
                group_config=group_config,
            )


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
