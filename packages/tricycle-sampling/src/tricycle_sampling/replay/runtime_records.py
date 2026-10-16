"""Live trajectory storage, explicit migration, and external-program workspaces."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from tricycle_core import BohrJobSpec
from tricycle_core import record_io as rio
from tricycle_core.record_ledger import FILENAME, RecordLedger, digest


def source_hashes() -> dict[str, str]:
    """Persistence implementation identity, independent of scientific model identity."""
    from tricycle_core import record_ledger
    core = Path(record_ledger.__file__).parent
    hashes = {name: digest((core / name).read_bytes()) for name in
            ("record_ledger.py", "record_io.py", "asset_store.py", "io.py", "toml.py", "transfer.py")}
    hashes.update({"replay/" + name: digest((Path(__file__).parent / name).read_bytes())
                   for name in ("runtime_records.py", "agent_records.py")})
    return hashes


def local_store(root: Path | None = None) -> Path:
    directory = (root or Path(os.environ.get("TRICYCLE_RECORDS_ROOT") or str(Path.cwd() / ".tricycle/record-ledgers"))).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    command = ["stat", "-f", "%T", str(directory)] if platform.system() == "Darwin" else ["stat", "-f", "-c", "%T", str(directory)]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    kind = result.stdout.strip().lower()
    if kind.startswith(("nfs", "smb", "cifs", "fuse", "osxfuse")):
        raise ValueError("Live SQLite records require local storage; set TRICYCLE_RECORDS_ROOT to a local directory")
    return directory


def initialize(seed: Path, *, storage_root: Path | None = None) -> RecordLedger:
    """Activate an empty seed namespace; existing histories require explicit migration."""
    if any(p.name != ".lock" for p in seed.iterdir()):
        raise ValueError("Initialize requires an empty seed; use migrate for existing evidence")
    database = local_store(storage_root) / (uuid.uuid4().hex + ".sqlite")
    ledger = RecordLedger.create(database)
    (seed / FILENAME).symlink_to(database)
    return ledger


def work_directory(current: Path, role: str) -> Path:
    """A persistent local handoff referenced by the ledger, outside run records."""
    found = rio.location(current)
    if found is None:
        raise ValueError("A managed handoff requires live records")
    if role not in {"agent", "review-agent", "download", "submission"}:
        raise ValueError("Unknown external handoff role")
    receipt = current / "runtime-work" / (role + ".json")
    if rio.exists(receipt):
        return Path(json.loads(rio.read_text(receipt))["directory"])
    workspace = found[0].database.parent / "work" / found[0].database.stem / uuid.uuid4().hex
    workspace.mkdir(parents=True, exist_ok=False)
    rio.write_json(receipt, {"role": role, "directory": str(workspace), "logical_directory": str(current)})
    return workspace


@contextmanager
def submission_handoff(current: Path, spec: BohrJobSpec) -> Iterator[BohrJobSpec]:
    if rio.location(current) is None:
        yield spec
        return
    with tempfile.TemporaryDirectory(prefix="tricycle-submit-manifest-") as temporary:
        manifest = Path(temporary) / "input-assets.json"
        manifest.write_bytes(rio.read_bytes(current / "input-assets.json"))
        yield replace(spec, parameters={**spec.parameters, "input_assets_manifest": str(manifest)})


def _inventory(seed: Path) -> list[dict[str, Any]]:
    result = []
    for directory, folders, files in os.walk(seed, followlinks=False):
        for name in sorted([*folders, *files]):
            path = Path(directory) / name
            if path.name.endswith(".lock") or path == seed / FILENAME:
                continue
            relative = path.relative_to(seed).as_posix()
            info = path.lstat()
            value: dict[str, Any] = {"path": relative, "mode": stat.S_IMODE(info.st_mode)}
            if path.is_symlink():
                value.update(kind="link", target=os.readlink(path))
            elif path.is_dir():
                value.update(kind="directory")
            elif path.is_file():
                value.update(kind="file", sha256=digest(path.read_bytes()), size=info.st_size)
            else:
                raise ValueError(f"Unsupported execution record: {path}")
            result.append(value)
    return sorted(result, key=lambda item: (len(Path(item["path"]).parts), item["path"]))


def migrate(seed: Path, *, storage_root: Path | None = None) -> dict[str, Any]:
    """Explicit migration under the controller lock; source bytes never regenerated.

    Stop supervisors/readers before switching layouts. A verified backup remains
    available even if pruning is interrupted. Repeating migration finishes pruning.
    """
    with (seed / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        found = rio.location(seed)
        if found:
            ledger = found[0]
            if ledger.get("storage-migration.json") is None:
                return ledger.verify()
            receipt = json.loads(ledger.read("storage-migration.json"))
            inventory = receipt["source_inventory"]
        else:
            if (seed / FILENAME).exists():
                raise ValueError("Restore a sealed historical container before live migration")
            inventory = _inventory(seed)
            database = local_store(storage_root) / (uuid.uuid4().hex + ".sqlite")
            ledger = RecordLedger.create(database)
            with ledger.transaction():
                for entry in inventory:
                    name = entry["path"]
                    if entry["kind"] == "directory":
                        ledger.mkdir(name, parents=True, exist_ok=True, mode=entry["mode"])
                    elif entry["kind"] == "link":
                        ledger.link(name, entry["target"])
                    else:
                        data = (seed / name).read_bytes()
                        if digest(data) != entry["sha256"]:
                            raise ValueError("Migration source changed")
                        ledger.put(name, data, mode=entry["mode"])
                ledger.put("storage-migration.json", (json.dumps({"source_root": str(seed),
                           "source_inventory": inventory, "schema": "tricycle.live-record-migration/v1"},
                           sort_keys=True, indent=2) + "\n").encode(), exclusive=True)
            ledger.verify()
            backup = database.with_suffix(".migration.sqlite")
            ledger.backup(backup)
            # The backup is one immutable file, not a duplicate expanded run.
            (seed / FILENAME).symlink_to(database)
        expected = {entry["path"]: entry for entry in inventory}
        remaining = _inventory(seed)
        for entry in remaining:
            if expected.get(entry["path"]) != entry:
                raise ValueError(f"Migration would remove changed or unrecognized evidence: {entry['path']}")
        ledger.verify()
        for directory in (seed, ledger.database.parent):
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        for entry in reversed(remaining):
            path = seed / entry["path"]
            if entry["kind"] == "directory":
                if not any(path.iterdir()):
                    path.rmdir()
            else:
                path.unlink()
        return ledger.verify()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("migrate", "verify", "list", "read", "snapshot", "recover-agent"))
    parser.add_argument("--seed-dir", type=Path, required=True)
    parser.add_argument("--storage-root", type=Path)
    parser.add_argument("--key")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--review", action="store_true")
    args = parser.parse_args()
    if args.command == "migrate":
        print(json.dumps(migrate(args.seed_dir, storage_root=args.storage_root)))
        return
    found = rio.location(args.seed_dir)
    if found is None:
        raise ValueError("No live trajectory container")
    ledger = found[0]
    if args.command == "verify":
        print(json.dumps(ledger.verify()))
    elif args.command == "list":
        print("\n".join(ledger.entries()))
    elif args.command == "read":
        if not args.key:
            parser.error("read requires --key")
        print(ledger.read(args.key).decode())
    elif args.command == "recover-agent":
        if not args.key:
            parser.error("recover-agent requires --key round-NNNN/decision")
        from tricycle_core.record_ledger import valid_name

        from .agent_records import recover_finished
        valid_name(args.key)
        with (args.seed_dir / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(recover_finished(args.seed_dir / args.key, review=args.review))
    else:
        if args.out is None:
            parser.error("snapshot requires --out")
        ledger.backup(args.out)


if __name__ == "__main__":
    main()
