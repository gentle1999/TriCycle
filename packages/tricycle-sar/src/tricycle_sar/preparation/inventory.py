"""Prepare a read-only environment inventory worker without installing models."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from tricycle_core.io import file_sha256
from tricycle_core.toml import dump_toml, load_toml

from tricycle_sar.diagnostics import inventory


def prepare(config_path: Path, output: Path) -> Path:
    config = load_toml(config_path)
    for field in ("project_id", "image", "machine_type"):
        if not config.get(field):
            raise ValueError(f"Inventory requires {field}")
    if output.exists():
        raise FileExistsError(output)
    common = output / "common"
    common.mkdir(parents=True)
    probe = common / "inventory.py"
    shutil.copy2(Path(inventory.__file__), probe)
    worker = common / "__bohr_task_script.sh"
    worker.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\n'
        'exec python3 "$PWD/common/inventory.py" --out-dir "$PWD/results"\n'
    )
    worker.chmod(0o755)
    task = output / "task.toml"
    dump_toml(task, {"phase": "environment_inventory", "training_allowed": False,
                     "inventory_sha256": file_sha256(probe)})
    request = {**config, "task_patterns": [str(task.resolve())], "workers": 1,
               "script": str(worker.resolve()), "common_paths": [str(probe.resolve())]}
    destination = output / "bohr-inventory.toml"
    dump_toml(destination, request)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.config, args.out_dir))


if __name__ == "__main__":
    main()
