"""Execute a script/console entrypoint inside the actual-process CUDA guard."""

from __future__ import annotations

import argparse
import importlib.metadata
import runpy
import sys
from pathlib import Path

from sar_accelerator import AcceleratorGuard  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--expected", choices=["cpu", "cuda"], required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--mode", choices=["train", "smoke", "predict"], required=True)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--script", type=Path)
    target.add_argument("--entrypoint")
    p.add_argument("args", nargs=argparse.REMAINDER)
    args = p.parse_args()
    argv = args.args[1:] if args.args[:1] == ["--"] else args.args
    with AcceleratorGuard(
        args.expected, args.report, training=args.mode != "predict", optimizer=args.mode == "train"
    ):
        if args.script:
            sys.argv = [str(args.script), *argv]
            try:
                runpy.run_path(str(args.script), run_name="__main__")
            except SystemExit as error:
                if error.code not in (0, None):
                    raise
        else:
            matches = list(
                importlib.metadata.entry_points(group="console_scripts", name=args.entrypoint)
            )
            if len(matches) != 1:
                raise RuntimeError(f"Expected one entrypoint: {args.entrypoint}")
            sys.argv = [args.entrypoint, *argv]
            try:
                result = matches[0].load()()
            except SystemExit as error:
                if error.code not in (0, None):
                    raise
                result = None
            if result not in (0, None):
                raise RuntimeError(f"Entrypoint failed: {result}")


if __name__ == "__main__":
    main()
