import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import zlib
from pathlib import Path

import pytest
from tricycle_sampling.replay.record_store import (
    FILENAME,
    RecordStore,
    scientific_view,
    seal,
    watch,
)


def trajectory(tmp_path: Path) -> Path:
    seed = tmp_path / "experiment/seed-00"
    seed.mkdir(parents=True)
    (seed / "state.json").write_text('{"seed":0,"phase":"complete"}')
    for name, data in {
        "round-0000/evaluation-summary.json": b'{"round_index":0}',
        "round-0000/result-asset-reference.json": b'{"sha256":"shared-reference"}',
        "round-0000/decision/response.json": b'{"rationale":"Accepted decision"}',
        "round-0000/decision/rejected-attempts/0001/agent.log": "Original failure: 中文\n".encode(),
        "round-0001/decision/response.json": b'{"rationale":"Accepted decision"}',
        "recovery/authorization.json": b'{"reason":"Authorized retry"}',
        "round-0001/launch.sh": b"#!/bin/sh\nexit 1\n",
    }.items():
        path = seed / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (seed / "round-0001/launch.sh").chmod(0o755)
    (seed / "round-0000/empty").mkdir()
    (seed / "round-0000/result").symlink_to(tmp_path / "unavailable-shared-assets")
    return seed


def test_lossless_consolidation_preserves_history_links_and_executable_modes(tmp_path: Path) -> None:
    seed = trajectory(tmp_path)
    originals = {p.relative_to(seed).as_posix(): p.read_bytes()
                 for p in seed.rglob("*") if p.is_file() and not p.is_symlink()}
    result = seal(seed)
    assert result["files"] == len(originals)
    assert not (seed / "round-0000").exists()
    assert (seed / "state.json").read_bytes() == originals["state.json"]
    with RecordStore(seed / FILENAME) as store:
        for name, data in originals.items():
            assert store.read(name) == data
        restored = tmp_path / "restored"
        store.restore(restored, links=True)
        assert (restored / "round-0000/empty").is_dir()
        assert os.readlink(restored / "round-0000/result") == str(tmp_path / "unavailable-shared-assets")
        assert (restored / "round-0001/launch.sh").stat().st_mode & 0o777 == 0o755
        assert store.connection.execute("SELECT count(*) FROM blobs").fetchone()[0] < result["files"]
    assert seal(seed) == result


def test_export_view_reads_sealed_evidence_without_expanding_durable_tree(tmp_path: Path) -> None:
    seed = trajectory(tmp_path)
    expected = (seed / "round-0000/decision/response.json").read_bytes()
    seal(seed)
    with scientific_view(seed) as view:
        assert view != seed
        assert (view / "round-0000/decision/response.json").read_bytes() == expected
        assert (view / "round-0000/evaluation-summary.json").is_file()
        assert not (view / "round-0000/result").exists()
    assert not view.exists()
    assert not (seed / "round-0000").exists()


def test_live_seed_or_live_pilot_consumer_cannot_be_sealed(tmp_path: Path) -> None:
    seed = trajectory(tmp_path)
    sibling = seed.parent / "seed-01"
    sibling.mkdir()
    (sibling / "state.json").write_text('{"seed":1,"phase":"monitor"}')
    with pytest.raises(ValueError, match="All experiment seeds"):
        seal(seed)
    assert not (seed / FILENAME).exists()
    assert (seed / "round-0000/evaluation-summary.json").exists()


def test_controller_lock_prevents_sealing(tmp_path: Path) -> None:
    seed = trajectory(tmp_path)
    with (seed / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            seal(seed)


def test_failed_durable_publication_keeps_expanded_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed = trajectory(tmp_path)
    original = os.fsync

    def fail_directory_sync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory sync failed")
        original(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_directory_sync)
        with pytest.raises(OSError, match="directory sync failed"):
            seal(seed)
    assert (seed / "round-0000/decision/response.json").exists()
    seal(seed)
    assert not (seed / "round-0000").exists()


@pytest.mark.parametrize("name", [".venv/pyvenv.cfg", "renamed/pyvenv.cfg", ".env", ".cache/data"])
def test_runtime_and_credentials_are_rejected_without_removing_evidence(tmp_path: Path, name: str) -> None:
    seed = trajectory(tmp_path)
    path = seed / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("do not retain")
    with pytest.raises(ValueError):
        seal(seed)
    assert path.exists()
    assert not (seed / FILENAME).exists()


def test_corruption_is_detected_before_restore_is_published(tmp_path: Path) -> None:
    seed = trajectory(tmp_path)
    seal(seed)
    with sqlite3.connect(seed / FILENAME) as connection:
        connection.execute("UPDATE blobs SET data = ?", (zlib.compress(b"tampered"),))
    with RecordStore(seed / FILENAME) as store, pytest.raises(ValueError, match="hash mismatch"):
        store.restore(tmp_path / "bad-restore")
    assert not (tmp_path / "bad-restore").exists()


def test_interrupted_pruning_resumes_but_changed_evidence_is_not_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed = trajectory(tmp_path)
    original = Path.unlink

    def interrupted(path: Path, missing_ok: bool = False) -> None:
        if path.name == "response.json":
            raise OSError("simulated interruption")
        original(path, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupted)
        with pytest.raises(OSError, match="simulated interruption"):
            seal(seed)
    assert (seed / FILENAME).exists()
    surviving = seed / "round-0000/decision/response.json"
    data = surviving.read_bytes()
    surviving.write_text("changed evidence")
    with pytest.raises(ValueError, match="differs from container"):
        seal(seed)
    assert surviving.read_text() == "changed evidence"
    surviving.write_bytes(data)
    seal(seed)
    assert not surviving.exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "round-0000/result/escape"])
def test_forged_inventory_cannot_escape_or_write_through_links(tmp_path: Path, name: str) -> None:
    seed = trajectory(tmp_path)
    seal(seed)
    with sqlite3.connect(seed / FILENAME) as connection:
        entry = json.loads(connection.execute("SELECT metadata FROM entries WHERE path = 'state.json'").fetchone()[0])
        entry["path"] = name
        connection.execute("INSERT INTO entries VALUES (?, ?)", (name, json.dumps(entry)))
        inventory = {key: json.loads(value) for key, value in connection.execute("SELECT path, metadata FROM entries")}
        digest = hashlib.sha256(json.dumps(inventory, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        connection.execute("UPDATE metadata SET value = ? WHERE key = 'inventory_sha256'", (digest,))
    with pytest.raises(ValueError):
        RecordStore(seed / FILENAME)


def test_watch_finalizes_only_after_controller_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tricycle_sampling.replay import campaign

    seed = trajectory(tmp_path)
    assert watch(seed.parent, live=False, allow_submit=False) == []
    assert not (seed / FILENAME).exists()
    (seed / "state.json").write_text('{"seed":0,"phase":"monitor"}')

    def finish(root: Path, *, live: bool, allow_submit: bool, once: bool) -> None:
        assert root == seed.parent and live and allow_submit and not once
        (seed / "state.json").write_text('{"seed":0,"phase":"complete"}')

    monkeypatch.setattr(campaign, "supervise", finish)
    assert len(watch(seed.parent, live=True, allow_submit=True)) == 1
    assert (seed / FILENAME).exists()
    assert not (seed / "round-0000").exists()


def test_blocked_watch_retains_working_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tricycle_sampling.replay import campaign

    seed = trajectory(tmp_path)
    (seed / "state.json").write_text('{"seed":0,"phase":"blocked"}')
    monkeypatch.setattr(campaign, "supervise", lambda *args, **kwargs: None)
    assert watch(seed.parent, live=True, allow_submit=True) == []
    assert (seed / "round-0000").exists()
    assert not (seed / FILENAME).exists()
