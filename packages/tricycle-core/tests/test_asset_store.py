from pathlib import Path

import pytest
from tricycle_core.asset_store import (
    externalize_tree,
    materialize_input,
    put_file,
    put_tree,
    resolve_file,
    resolve_tree,
)
from tricycle_core.io import write_json


def test_sources_remain_independent_while_trees_share_identical_blobs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "weights").write_bytes(b"immutable model")
    store = tmp_path / "store"
    first = put_tree(source, store)
    (source / "extra").write_text("different tree")
    second = put_tree(source, store)
    a, b = resolve_tree(first), resolve_tree(second)
    assert (a / "weights").stat().st_ino == (b / "weights").stat().st_ino
    assert (source / "weights").stat().st_ino != (a / "weights").stat().st_ino
    (source / "weights").write_bytes(b"edited checkout")
    assert (a / "weights").read_bytes() == b"immutable model"
    assert put_tree(a, store) == first


def test_compact_reference_materializes_an_independent_worker(tmp_path: Path) -> None:
    source = tmp_path / "assets"
    source.mkdir()
    (source / "run.sh").write_text("#!/bin/sh\n")
    (source / "empty/nested").mkdir(parents=True)
    reference = externalize_tree(source, tmp_path / "store")
    assert source.is_symlink()
    checkpoint = tmp_path / "checkpoint"
    checkpoint.write_bytes(b"trained")
    file_ref = put_file(checkpoint, tmp_path / "store")
    overlay = tmp_path / "round"
    overlay.mkdir()
    (overlay / "request.json").write_text("{}")
    manifest = tmp_path / "input-assets.json"
    write_json(manifest, {"schema": "tricycle.asset-input/v1", "trees": [reference],
                          "files": {"previous.pt": file_ref}})
    upload = tmp_path / "upload"
    materialize_input(manifest, overlay, upload)
    assert (upload / "empty/nested").is_dir()
    assert (upload / "previous.pt").read_bytes() == b"trained"
    assert (upload / "previous.pt").stat().st_ino != resolve_file(file_ref).stat().st_ino
    (upload / "previous.pt").write_bytes(b"worker modification")
    assert resolve_file(file_ref).read_bytes() == b"trained"


def test_directory_structure_is_hashed_and_verified(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    first = put_tree(source, tmp_path / "store")
    (source / "empty").mkdir()
    second = put_tree(source, tmp_path / "store")
    assert first["sha256"] != second["sha256"]
    (resolve_tree(second) / "empty").rmdir()
    with pytest.raises(ValueError, match="directory set"):
        resolve_tree(second)


def test_tampering_is_rejected_before_materialization(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_text("frozen")
    reference = put_tree(source, tmp_path / "store")
    path = resolve_tree(reference) / "data"
    path.chmod(0o644)
    path.write_text("tampered")
    with pytest.raises(ValueError, match="changed"):
        resolve_tree(reference)


@pytest.mark.parametrize("name", ["../escape", "/absolute"])
def test_input_manifest_rejects_path_traversal(tmp_path: Path, name: str) -> None:
    source = tmp_path / "source"
    source.write_text("data")
    ref = put_file(source, tmp_path / "store")
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"schema": "tricycle.asset-input/v1", "trees": [], "files": {name: ref}})
    with pytest.raises(ValueError, match="Unsafe"):
        materialize_input(manifest, overlay, tmp_path / "upload")


def test_input_record_has_no_payload_or_links_and_materializes_executable_files(tmp_path: Path) -> None:
    from tricycle_core.asset_store import freeze_inputs, resolve_input

    run = tmp_path / "run"
    common = run / "common"
    (common / "empty").mkdir(parents=True)
    (common / "data.csv").write_text("frozen dataset")
    script = run / "worker.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    freeze_inputs(run, (common, script), tmp_path / "store")
    assert not common.exists() and not common.is_symlink()
    assert not script.exists()
    assert resolve_input(run, "common/data.csv").read_text() == "frozen dataset"
    execution = tmp_path / "execution"
    materialize_input(run / "input-assets.json", None, execution)
    assert (execution / "common/empty").is_dir()
    assert (execution / "worker.sh").stat().st_mode & 0o111
    (execution / "common/data.csv").write_text("changed by worker")
    assert resolve_input(run, "common/data.csv").read_text() == "frozen dataset"


def test_failed_input_publication_keeps_the_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tricycle_core.asset_store import freeze_inputs

    run = tmp_path / "run"
    run.mkdir()
    source = run / "weights.pt"
    source.write_bytes(b"weights")

    def fail(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("tricycle_core.asset_store.write_json", fail)
    with pytest.raises(OSError, match="disk full"):
        freeze_inputs(run, (source,), tmp_path / "store")
    assert source.read_bytes() == b"weights"
    assert not source.is_symlink()
