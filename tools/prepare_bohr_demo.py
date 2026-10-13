#!/usr/bin/env python3
"""Prepare local RitS and RxnResid demo bundles using their shared package contracts.

This demo uses the current sampling experiment schema, with user-supplied component
and path IDs. RxnResid general train/refit/infer accepts native configs independently
of the reaction database.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from tricycle_rits.preparation import prepare_demo as prepare_rits_demo
from tricycle_sar.preparation.demo import prepare_demo as prepare_sar_demo


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rits-export", type=Path, required=True)
    parser.add_argument("--rxnresid-data", "--rxnresid-export", dest="rxnresid_export", type=Path, required=True, help="CSV for the current sampling demo, from any source")
    parser.add_argument("--rits-source", type=Path, required=True)
    parser.add_argument("--rxnresid-source", type=Path, required=True)
    parser.add_argument("--rits-checkpoint", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    output = args.out_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    rits = prepare_rits_demo(
        export_path=args.rits_export.resolve(),
        source=args.rits_source.resolve(),
        checkpoint=args.rits_checkpoint.resolve(),
        root=output / "rits",
    )
    rxnresid = prepare_sar_demo(
        export_path=args.rxnresid_export.resolve(),
        source=args.rxnresid_source.resolve(),
        root=output / "rxnresid",
    )
    print({"rits": rits, "rxnresid": rxnresid})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
