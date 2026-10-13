"""Prepare per-model Bohr environments. Default plan is offline and read-only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    from tricycle_sar.backends import list_backends

    parser.add_argument(
        "--model", choices=list_backends(), required=True
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--assets-directory", type=Path)
    args = parser.parse_args()
    from tricycle_sar.environment.validated import initialize_backend, plan_backend

    result = (
        initialize_backend(args.model, args.root, assets_directory=args.assets_directory)
        if args.execute
        else {**plan_backend(args.model, args.root), "status": "plan"}
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result["status"] == "dependency-conflicts":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
