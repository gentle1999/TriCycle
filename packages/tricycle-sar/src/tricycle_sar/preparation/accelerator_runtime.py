"""Stage the verified common accelerator runtime for an independent worker."""
from __future__ import annotations

import shutil
from pathlib import Path

from tricycle_core.io import file_sha256
from tricycle_core.toml import dump_toml
from tricycle_core.training_protocol import protocol_file_source

from tricycle_sar import get_backend
from tricycle_sar.backends.registry import CATALOG


def stage_runtime(bundle: Path) -> None:
    backend = get_backend("rxnresid")
    entries = {entry["path"]: entry for entry in backend.method["files"]}
    prefix = f"common/{backend.method['bundle_name']}/scripts/"
    names = ("sar_accelerator.py", "sar_accelerator_exec.py", "sar_stage.py")
    sources = {f"scripts/{name}": entries[prefix + name] for name in names}
    sources.update({f"scripts/sar_runtime/{name}.py": entries[f"common/sar_runtime/{name}.py"]
                    for name in ("io", "errors", "toml", "metrics", "training_protocol")})
    for relative, entry in sources.items():
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(protocol_file_source(CATALOG, entry), target)
    dump_toml(bundle / "accelerator-runtime.toml", {"files": [
        {"path": relative, "sha256": file_sha256(bundle / relative)} for relative in sources
    ]})
