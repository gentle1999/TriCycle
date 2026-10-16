"""Shared, offline reproduction commands for compact SAR result packages."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .results import index_assets, prepare_training, publish_recorded, verify


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    recorded = commands.add_parser("publish-recorded")
    for name in ("run-record", "dataset", "splits", "predictions", "out-dir"):
        recorded.add_argument(f"--{name}", type=Path, required=True)
    for name in ("verify", "prepare", "assets"):
        child = commands.add_parser(name)
        child.add_argument("--result-dir", type=Path, required=True)
        if name != "verify":
            child.add_argument("--out-dir", type=Path, required=True)
        if name == "prepare":
            child.add_argument("--model", required=True)
            child.add_argument("--fold", type=int, required=True)
            child.add_argument("--asset-dir", type=Path)
            child.add_argument("--retain-checkpoint", action="store_true")
        elif name == "assets":
            child.add_argument("--file", type=Path, action="append", required=True)
    args = parser.parse_args(argv)
    if args.command == "publish-recorded":
        result = {"result_dir": str(publish_recorded(args.run_record, args.dataset, args.splits,
                                                    args.predictions, args.out_dir))}
    elif args.command == "verify":
        result = verify(args.result_dir)
    elif args.command == "prepare":
        result = prepare_training(
            args.result_dir,
            args.model,
            args.fold,
            args.out_dir,
            assets=args.asset_dir,
            retain_checkpoint=args.retain_checkpoint,
        )
    else:
        result = index_assets(args.result_dir, args.file, args.out_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
