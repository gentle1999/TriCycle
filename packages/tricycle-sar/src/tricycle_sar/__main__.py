"""SAR package CLI: validated backends, local task preparation, and compact results."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .backends import get_backend, list_backends


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "results":
        from .results_cli import main as results

        results(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "benchmark":
        from .benchmark_cli import main as benchmark

        benchmark(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("results", help="Verify results, prepare training inputs or index assets")
    commands.add_parser("benchmark", help="Prepare, check, collect or publish benchmarks")
    commands.add_parser("list")
    commands.add_parser("validate")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--model", required=True)
    prepare.add_argument("--dataset", type=Path, required=True)
    prepare.add_argument("--splits", type=Path, required=True)
    prepare.add_argument("--fold", type=int, required=True)
    prepare.add_argument("--out-dir", type=Path, required=True)
    prepare.add_argument("--asset-dir", type=Path)
    prepare.add_argument("--retain-checkpoint", action="store_true")
    requests = commands.add_parser("requests")
    requests.add_argument("--benchmark-run", type=Path, required=True)
    requests.add_argument("--out-dir", type=Path, required=True)
    requests.add_argument("--job-group-name", required=True)
    requests.add_argument("--group-config", type=Path, required=True)
    args = parser.parse_args()
    result: dict | list[dict]
    if args.command == "prepare":
        result = get_backend(args.model).prepare(
            args.dataset,
            args.splits,
            args.out_dir,
            fold=args.fold,
            assets=args.asset_dir,
            retain_checkpoint=args.retain_checkpoint,
        )
    elif args.command == "requests":
        from .preparation.all_models import prepare as prepare_requests

        result = {
            "requests": str(
                prepare_requests(
                    args.benchmark_run,
                    args.out_dir,
                    group_name=args.job_group_name,
                    group_config=args.group_config,
                )
            )
        }
    elif args.command == "list":
        result = [get_backend(model).environment | {"model": model} for model in list_backends()]
    else:
        from tricycle_core.training_protocol import resolve_training_registry, training_method

        from .backends.registry import CATALOG

        manifest, registry = resolve_training_registry(CATALOG)
        for identifier, method in registry["methods"].items():
            training_method(manifest, registry, method["model"], identifier)
        result = {
            "status": "verified",
            "models": len(registry["models"]),
            "methods": len(registry["methods"]),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
