from pathlib import Path
from unittest.mock import Mock

import pytest
from tricycle_core import InvalidRequestError
from tricycle_rits import OpenSshRitsGateway, RitsSshFinetuneRequest


def request(*, bundle_dir: str | None = None, download_paths: tuple[str, ...] = (),
            download_dir: str | None = None) -> RitsSshFinetuneRequest:
    return RitsSshFinetuneRequest(host="gpu.internal", remote_root="/jobs", bundle_dir=bundle_dir,
                                  download_paths=download_paths, download_dir=download_dir)


def test_dirty_bundle_is_rejected_before_remote_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "renamed-env").mkdir()
    (tmp_path / "renamed-env/pyvenv.cfg").write_text("home=/python")
    gateway = OpenSshRitsGateway()
    runner = Mock()
    monkeypatch.setattr(gateway, "_run", runner)
    with pytest.raises(InvalidRequestError, match="pyvenv.cfg"):
        gateway.submit(request(bundle_dir=str(tmp_path)), "job")
    runner.assert_not_called()


def test_environment_cannot_be_selected_for_download() -> None:
    with pytest.raises(InvalidRequestError):
        request(download_paths=("results/.venv",))


@pytest.mark.parametrize("dirty", [True, False])
def test_selected_remote_directory_is_checked_before_publication(
    tmp_path: Path, dirty: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "download"
    config = request(download_paths=("results",), download_dir=str(destination))
    gateway = OpenSshRitsGateway()
    staging_paths = []

    def download(args: list[str], **_: object) -> str:
        staging = Path(args[-1])
        staging_paths.append(staging)
        (staging / "results").mkdir()
        (staging / "results/metrics.csv").write_text("mae\n1\n")
        if dirty:
            (staging / "results/.cache").mkdir()
            (staging / "results/.cache/installed.bin").write_bytes(b"runtime")
        return ""

    monkeypatch.setattr(gateway, "_run", Mock(side_effect=download))
    if dirty:
        with pytest.raises(InvalidRequestError):
            gateway.download(config, "job")
        assert not destination.exists()
    else:
        assert gateway.download(config, "job") == (str(destination / "results/metrics.csv"),)
    assert not staging_paths[0].exists()
