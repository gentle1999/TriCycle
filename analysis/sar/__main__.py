"""Offline SAR analysis and figures from verified scientific result packages."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    boxplot = commands.add_parser("boxplot")
    boxplot.add_argument("--run-dir", type=Path, required=True)
    boxplot.add_argument("--prediction-dir", type=Path, required=True)
    boxplot.add_argument("--out-dir", type=Path, required=True)
    boxplot.add_argument("--xscale", choices=("linear", "log"), default="log")
    boxplot.add_argument("--aspect-ratio", type=float, default=2.0)
    boxplot.add_argument("--width-inches", type=float, default=7.2)
    for name in ("render", "distributions", "collection-distributions"):
        child = commands.add_parser(name)
        if name == "collection-distributions":
            child.add_argument("--dataset", type=Path, required=True)
            child.add_argument("--aspect-ratio", type=float, default=4.0)
            child.add_argument("--width-inches", type=float, default=8.0)
        else:
            child.add_argument("--result-dir", type=Path, required=True)
        child.add_argument("--out-dir", type=Path, required=True)
        if name == "distributions":
            child.add_argument("--style", choices=("analysis", "publication"), default="analysis")
    args = parser.parse_args(argv)
    if args.command == "boxplot":
        from .boxplot import render_run_boxplot

        print(json.dumps(render_run_boxplot(
            args.run_dir, args.prediction_dir, args.out_dir, xscale=args.xscale,
            aspect_ratio=args.aspect_ratio, width_inches=args.width_inches,
        ), indent=2))
        return
    if args.command == "collection-distributions":
        from .distribution_figures import render_collection

        result = render_collection(
            args.dataset, args.out_dir, aspect_ratio=args.aspect_ratio, width_inches=args.width_inches
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    from .figures import render_distributions, render_results

    result = (
        render_results(args.result_dir, args.out_dir)
        if args.command == "render"
        else render_distributions(args.result_dir, args.out_dir, style=args.style)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
