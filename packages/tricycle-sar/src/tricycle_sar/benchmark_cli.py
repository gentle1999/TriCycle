"""Local preparation, verification and publication of SAR model benchmarks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tricycle_sar.modelling_benchmark import (
    check,
    collect,
    prepare_modelling,
    publish_results,
)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--config", type=Path, required=True)
    prepare_parser.add_argument("--run-dir", type=Path)
    prepare_parser.add_argument("--rxngraphormer-assets", type=Path)
    prepare_parser.add_argument("--reactiont5-assets", type=Path)
    prepare_parser.add_argument("--uam-assets", type=Path)
    prepare_parser.add_argument("--molclr-assets", type=Path)
    for name in ("check", "collect", "publish"):
        child = sub.add_parser(name)
        child.add_argument("--run-dir", type=Path, required=True)
        if name == "publish":
            child.add_argument("--out-dir", type=Path, required=True)
        if name in {"collect", "publish"}:
            child.add_argument("--prediction-dir", type=Path)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = {
            "run_dir": str(
                prepare_modelling(
                    args.config,
                    run_dir=args.run_dir,
                    rxngraphormer_assets=args.rxngraphormer_assets,
                    reactiont5_assets=args.reactiont5_assets,
                    molecular_assets={
                        k: v
                        for k, v in {"uam": args.uam_assets, "molclr": args.molclr_assets}.items()
                        if v is not None
                    },
                )
            )
        }
    elif args.command == "check":
        result = check(args.run_dir)
    elif args.command == "collect":
        result = collect(args.run_dir, prediction_dir=args.prediction_dir)
    else:
        result = {
            "result_dir": str(
                publish_results(args.run_dir, args.out_dir, prediction_dir=args.prediction_dir)
            )
        }
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
