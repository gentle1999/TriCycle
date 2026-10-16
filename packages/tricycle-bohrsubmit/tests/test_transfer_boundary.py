import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest
from tricycle_bohrsubmit.gateway import BohrCliConfig, BohrCliGateway
from tricycle_bohrsubmit.staging import _copy_path
from tricycle_core import BohrJobSpec, InvalidRequestError


def job(**parameters: object) -> BohrJobSpec:
    return BohrJobSpec(name="transfer-test", operation="test", image="test",
                       project_id="1", machine_type="cpu", parameters=parameters)


@pytest.mark.parametrize("selectors", [[], ["*"], ["."], [".venv/"], ["results/**"]])
def test_bad_download_scope_blocks_submission_before_cli(
    selectors: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = BohrCliGateway(environ={})
    runner = Mock()
    monkeypatch.setattr(gateway, "_run", runner)
    with pytest.raises(InvalidRequestError):
        gateway.submit(job(backward_files=selectors))
    runner.assert_not_called()


def test_runtime_input_blocks_batch_staging_and_direct_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "bundle"
    (source / ".venv/lib").mkdir(parents=True)
    (source / ".venv/lib/runtime.txt").write_text("runtime")
    gateway = BohrCliGateway(environ={})
    runner = Mock()
    monkeypatch.setattr(gateway, "_run", runner)
    with pytest.raises(InvalidRequestError):
        _copy_path(source, tmp_path / "staged")
    with pytest.raises(InvalidRequestError):
        gateway.submit(job(input_directory=str(source), backward_files=["results/"]))
    runner.assert_not_called()
    assert not (tmp_path / "staged").exists()


def test_extra_cli_arguments_cannot_override_transfer_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = BohrCliGateway(environ={})
    runner = Mock()
    monkeypatch.setattr(gateway, "_run", runner)
    with pytest.raises(InvalidRequestError, match="override"):
        gateway.submit(job(backward_files=["results/"], extra_submit_args=["--config_file=other.json"]))
    runner.assert_not_called()


@pytest.mark.parametrize("dirty", [True, False])
def test_download_checks_archive_before_publishing_and_cleans_staging(
    tmp_path: Path, dirty: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = BohrCliGateway(BohrCliConfig(download_retry_mode="none"), environ={})
    downloaded = []

    def download(args: list[str], **_: object) -> str:
        staging = Path(args[args.index("--out") + 1])
        downloaded.append(staging)
        with zipfile.ZipFile(staging / "out.zip", "w") as archive:
            archive.writestr("results/metrics.json", "{}")
            if dirty:
                archive.writestr("results/.venv/pyvenv.cfg", "home=/python")
        return ""

    monkeypatch.setattr(gateway, "_run", Mock(side_effect=download))
    destination = tmp_path / "download"
    if dirty:
        with pytest.raises(InvalidRequestError, match="Runtime environment/cache"):
            gateway.download("job-id", destination)
        assert not destination.exists()
    else:
        assert gateway.download("job-id", destination) == (str(destination / "out.zip"),)
        with zipfile.ZipFile(destination / "out.zip") as archive:
            assert archive.read("results/metrics.json") == b"{}"
    assert not downloaded[0].exists()
