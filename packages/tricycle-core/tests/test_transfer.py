import io
import tarfile
import zipfile
from pathlib import Path

import pytest
from tricycle_core.asset_store import materialize_input, put_tree
from tricycle_core.errors import InvalidRequestError
from tricycle_core.io import write_json
from tricycle_core.transfer import (
    publish_download,
    validate_result_selector,
    validate_transfer_tree,
)


@pytest.mark.parametrize("name", [
    ".venv/lib/python/site.py", "tools/venv/bin/python", "renamed-env/pyvenv.cfg",
    "env/conda-meta/history", "results/.cache/pip/wheel", "pkg/__pycache__/x.pyc",
    "lib/python3.11/site-packages/x.py", ".git/config", "model-envs/.pip-cache/wheel",
])
def test_runtime_inputs_are_rejected_with_the_offending_path(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_text("synthetic runtime")
    with pytest.raises(InvalidRequestError, match="Runtime environment/cache"):
        validate_transfer_tree(tmp_path)


@pytest.mark.parametrize("selector", ["", ".", "./", "*", "**", "*.*", "*/result.csv",
                                       "results/**", ".venv/", "results/.cache/", "../x", "/results"])
def test_overbroad_or_runtime_result_selectors_are_rejected(selector: str) -> None:
    with pytest.raises(InvalidRequestError):
        validate_result_selector(selector)


@pytest.mark.parametrize("selector", ["results/", "outputs/round/", "worker.log", "*.xyz",
                                       "*.log", "results/*.tar", "outputs/model.pt"])
def test_explicit_results_and_scientific_files_remain_supported(selector: str) -> None:
    assert validate_result_selector(selector) == selector


@pytest.mark.parametrize("archive_name", ["out.zip", "source.tar.gz"])
def test_runtime_in_archive_is_rejected_before_publication(tmp_path: Path, archive_name: str) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    archive_path = staging / archive_name
    member = "results/renamed-env/pyvenv.cfg"
    if archive_name.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(member, "home = /python")
    else:
        with tarfile.open(archive_path, "w:gz") as archive:
            data = b"home = /python"
            info = tarfile.TarInfo(member)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    destination = tmp_path / "download"
    destination.mkdir()
    (destination / "prior.log").write_text("original evidence")
    with pytest.raises(InvalidRequestError, match="pyvenv.cfg"):
        publish_download(staging, destination)
    assert list(destination.iterdir()) == [destination / "prior.log"]
    assert (destination / "prior.log").read_text() == "original evidence"


def test_valid_result_archive_is_published_byte_for_byte_and_repeatable(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    archive_path = staging / "out.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("outputs/round/checkpoint.pt", b"model")
        archive.writestr("outputs/round/metrics.json", b"{}")
    destination = tmp_path / "download"
    publish_download(staging, destination)
    assert (destination / "out.zip").read_bytes() == archive_path.read_bytes()
    assert publish_download(staging, destination) == (str(destination / "out.zip"),)
    (destination / "out.zip").write_bytes(b"old evidence")
    with pytest.raises(InvalidRequestError, match="replace existing evidence"):
        publish_download(staging, destination)
    assert (destination / "out.zip").read_bytes() == b"old evidence"


def test_new_asset_tree_rejects_environment_without_changing_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / ".venv").mkdir(parents=True)
    (source / ".venv/pyvenv.cfg").write_text("home = /python")
    with pytest.raises(InvalidRequestError):
        put_tree(source, tmp_path / "store")
    assert (source / ".venv/pyvenv.cfg").read_text() == "home = /python"
    assert not (tmp_path / "store").exists()


def test_manifest_cannot_rename_valid_asset_to_environment_path(tmp_path: Path) -> None:
    from tricycle_core.asset_store import put_file

    source = tmp_path / "input.txt"
    source.write_text("data")
    ref = put_file(source, tmp_path / "store")
    manifest = tmp_path / "input-assets.json"
    write_json(manifest, {"schema": "tricycle.asset-input/v1", "trees": [],
                          "files": {".venv/hidden.txt": ref}})
    with pytest.raises(InvalidRequestError):
        materialize_input(manifest, None, tmp_path / "delivery")
    assert not (tmp_path / "delivery").exists()


def test_shared_views_are_allowed_but_cycles_are_rejected(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "weights.pt").write_bytes(b"model")
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    (delivery / "first").symlink_to(assets, target_is_directory=True)
    (delivery / "second").symlink_to(assets, target_is_directory=True)
    validate_transfer_tree(delivery)
    (delivery / "loop").symlink_to(delivery, target_is_directory=True)
    with pytest.raises(InvalidRequestError, match="Cyclic"):
        validate_transfer_tree(delivery)


def test_explicit_model_asset_can_originate_in_local_cache(tmp_path: Path) -> None:
    from tricycle_core.asset_store import put_file, resolve_tree

    cached = tmp_path / ".cache/models/weights.pt"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"model weights")
    assert put_file(cached, tmp_path / "store")["bytes"] == len(b"model weights")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "weights.pt").symlink_to(cached)
    validate_transfer_tree(bundle)
    reference = put_tree(bundle, tmp_path / "store")
    assert (resolve_tree(reference) / "weights.pt").read_bytes() == b"model weights"
