"""Offline SAR analysis and figures from verified scientific result packages."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("render", "distributions"):
        child = commands.add_parser(name)
        child.add_argument("--result-dir", type=Path, required=True)
        child.add_argument("--out-dir", type=Path, required=True)
        if name == "distributions":
            child.add_argument("--style", choices=("analysis", "publication"), default="analysis")
    args = parser.parse_args(argv)
    from .figures import render_distributions, render_results

    result = (
        render_results(args.result_dir, args.out_dir)
        if args.command == "render"
        else render_distributions(args.result_dir, args.out_dir, style=args.style)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
