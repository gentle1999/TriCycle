import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tricycle_bohrsubmit import BohrCliConfig, BohrCliGateway
from tricycle_bohrsubmit.gateway import BohrGatewayError
from tricycle_core import Artifact, BohrJobSpec, InvalidRequestError


class BohrCliGatewayTests(unittest.TestCase):
    def test_gateway_loads_dotenv_for_the_child_cli_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / ".env"
            env_file.write_text(
                "BOHR_ACCESS_KEY=file-secret\nBOHR_BIN=bohr-from-file\nBOHR_RETRY_MAX=3\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                gateway = BohrCliGateway(env_file=env_file)

            self.assertEqual(gateway.environ["BOHR_ACCESS_KEY"], "file-secret")
            self.assertEqual(gateway.config.executable, "bohr-from-file")
            self.assertEqual(gateway.config.retry_max, 3)
            self.assertNotIn("BOHR_ACCESS_KEY", os.environ)

    def test_submit_uses_public_job_id_and_current_output_flag(self) -> None:
        gateway = BohrCliGateway(
            BohrCliConfig(retry_delay=0, retry_jitter=0),
            environ={"BOHR_ACCESS_KEY": "test-key"},
        )
        job = BohrJobSpec(
            name="worker-001",
            operation="bohr.batch.worker",
            image="registry.example/chemistry:latest",
            command=("bash", "run_worker.sh"),
            project_id="123",
            machine_type="c2_m4_cpu",
            parameters={
                "input_directory": "./worker-001",
                "command_string": "bash run_worker.sh",
                "log_file": "worker.log",
                "job_group_id": "456",
            },
        )
        result = SimpleNamespace(
            returncode=0,
            output='{"ok":true,"data":{"jobId":11,"bohrJobId":22}}',
        )

        with patch.object(gateway, "_run_once", return_value=result) as run_once:
            self.assertEqual(gateway.submit(job), "22")

        args = run_once.call_args.args[0]
        self.assertIn("--project-id", args)
        self.assertNotIn("--project_id", args)
        self.assertIn("--output", args)
        self.assertEqual(args[args.index("--output") + 1], "json")
        self.assertNotIn("--backward_files", args)
        self.assertNotIn("--dataset_path", args)
        self.assertEqual(gateway.environ["BOHR_ACCESS_KEY"], "test-key")

    def test_job_id_parser_rejects_the_new_cli_internal_id_field(self) -> None:
        self.assertEqual(
            BohrCliGateway._job_id('{"ok":true,"data":{"id":11,"bohrId":22}}'),
            "22",
        )
        self.assertIsNone(BohrCliGateway._job_id('{"ok":true,"data":{"id":11}}'))
        self.assertEqual(BohrCliGateway._job_id("Bohr Job Id: 22"), "22")
        self.assertIsNone(BohrCliGateway._job_id("Job Id: 11"))

    def test_group_id_parser_prefers_the_submit_compatible_id(self) -> None:
        self.assertEqual(
            BohrCliGateway._group_id(
                '{"ok":true,"data":{"jobGroupId":11,"bohrJobGroupId":22}}'
            ),
            "22",
        )
        self.assertEqual(
            BohrCliGateway._group_id('{"ok":true,"data":{"jobGroupId":11,"groupId":22}}'),
            "22",
        )
        self.assertIsNone(BohrCliGateway._group_id('{"ok":true,"data":{"jobGroupId":11}}'))

    def test_status_and_download_use_current_bohr_cli_commands(self) -> None:
        gateway = BohrCliGateway(
            BohrCliConfig(retry_delay=0, retry_jitter=0),
            environ={},
        )
        result = SimpleNamespace(returncode=0, output='{"ok":true,"data":{"phase":"running"}}')
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "downloads"
            with patch.object(gateway, "_run_once", return_value=result) as run_once:
                self.assertEqual(gateway.status("22")["status"], "running")
                gateway.download("22", destination)
                gateway.log("22", destination / "logs")

        calls = [call.args[0] for call in run_once.call_args_list]
        self.assertEqual(calls[0], ["job", "describe", "--id", "22", "--output", "json"])
        self.assertEqual(calls[1], ["job", "download", "22", "--out", str(destination)])
        self.assertEqual(calls[2], ["job", "log", "22", "--out", str(destination / "logs")])

    def test_status_prefers_current_cli_phase_over_numeric_status(self) -> None:
        gateway = BohrCliGateway(
            BohrCliConfig(retry_delay=0, retry_jitter=0),
            environ={},
        )
        result = SimpleNamespace(
            returncode=0,
            output='{"ok":true,"data":{"status":-1,"phase":"failed"}}',
        )
        with patch.object(gateway, "_run_once", return_value=result):
            self.assertEqual(gateway.status("22")["status"], "failed")

    def test_structured_non_retryable_cli_error_is_not_retried(self) -> None:
        gateway = BohrCliGateway(
            BohrCliConfig(retry_max=5, retry_delay=0, retry_jitter=0),
            environ={},
        )
        result = SimpleNamespace(
            returncode=1,
            output=(
                '{"ok":false,"error":{"code":"RESOURCE_NOT_FOUND",'
                '"retryable":false}}'
            ),
        )
        with (
            patch.object(gateway, "_run_once", return_value=result) as run_once,
            self.assertRaises(BohrGatewayError) as raised,
        ):
            gateway.status("22")

        self.assertEqual(run_once.call_count, 1)
        self.assertIn("after 1 attempt(s)", str(raised.exception))

    def test_structured_retryable_cli_error_is_retried(self) -> None:
        gateway = BohrCliGateway(
            BohrCliConfig(retry_max=2, retry_delay=0, retry_jitter=0),
            environ={},
        )
        retryable = SimpleNamespace(
            returncode=1,
            output='{"ok":false,"error":{"code":"TEMPORARY","retryable":true}}',
        )
        success = SimpleNamespace(returncode=0, output='{"ok":true,"data":{"phase":"running"}}')
        with patch.object(gateway, "_run_once", side_effect=(retryable, success)) as run_once:
            self.assertEqual(gateway.status("22")["status"], "running")

        self.assertEqual(run_once.call_count, 2)

    def test_submit_recovery_ignores_an_older_job_with_the_same_name(self) -> None:
        gateway = BohrCliGateway(BohrCliConfig(retry_delay=0), environ={})
        created_after = datetime.fromisoformat("2026-09-28T00:15:00+08:00")
        response = json.dumps({"data": {"items": [
            {"bohrId": 22, "jobName": "fold-2", "createTime": "2026-09-28 00:02:41"},
        ]}})
        with patch.object(gateway, "_run", return_value=response):
            self.assertIsNone(
                gateway._find_recent_job("fold-2", "7447947", created_after=created_after)
            )

    def test_submit_recovery_matches_a_job_created_after_the_attempt_started(self) -> None:
        gateway = BohrCliGateway(BohrCliConfig(retry_delay=0), environ={})
        created_after = datetime.fromisoformat("2026-09-28T00:15:00+08:00")
        response = json.dumps({"data": {"items": [
            {"bohrId": 23, "jobName": "fold-2", "createTime": "2026-09-28 00:15:08"},
            {"bohrId": 22, "jobName": "fold-2", "createTime": "2026-09-28 00:02:41"},
        ]}})
        with patch.object(gateway, "_run", return_value=response):
            self.assertEqual(
                gateway._find_recent_job("fold-2", "7447947", created_after=created_after),
                "23",
            )

    def test_model_job_uploads_the_declared_input_directory(self) -> None:
        gateway = BohrCliGateway(
            BohrCliConfig(retry_delay=0, retry_jitter=0),
            environ={},
        )
        job = BohrJobSpec(
            name="rits-infer-smoke",
            operation="rits.infer",
            image="registry.dp.tech/dptech/dp/native/prod-23278/dl:260828",
            command=("bash", "run.sh"),
            project_id="123",
            machine_type="gpu-machine",
            parameters={"input_directory": "/tmp/rits-job"},
        )
        result = SimpleNamespace(
            returncode=0,
            output='{"ok":true,"data":{"bohrJobId":22}}',
        )

        with patch.object(gateway, "_run_once", return_value=result) as run_once:
            self.assertEqual(gateway.submit(job), "22")

        args = run_once.call_args.args[0]
        self.assertEqual(args[args.index("--input_directory") + 1], "/tmp/rits-job")
        self.assertEqual(args[args.index("--image_address") + 1], job.image)

    def test_submit_uses_cli_config_for_declared_output_artifacts(self) -> None:
        gateway = BohrCliGateway(
            BohrCliConfig(retry_delay=0, retry_jitter=0),
            environ={},
        )
        job = BohrJobSpec(
            name="rxnresid-train-smoke",
            operation="sar.train",
            image="registry.example/chemistry:latest",
            command=("bash", "run.sh"),
            project_id="123",
            machine_type="gpu-machine",
            output_artifacts=(
                Artifact(
                    "training-output",
                    "outputs/rxnresid-smoke",
                    kind="directory",
                ),
            ),
        )
        captured: dict[str, object] = {}

        def run_once(args: list[str]) -> SimpleNamespace:
            captured["args"] = args
            config_path = Path(args[args.index("--config_file") + 1])
            captured["config"] = json.loads(config_path.read_text(encoding="utf-8"))
            captured["config_path"] = config_path
            return SimpleNamespace(
                returncode=0,
                output='{"ok":true,"data":{"bohrJobId":22}}',
            )

        with patch.object(gateway, "_run_once", side_effect=run_once):
            self.assertEqual(gateway.submit(job), "22")

        args = captured["args"]
        self.assertIsInstance(args, list)
        self.assertEqual(
            captured["config"],
            {"backward_files": ["outputs/rxnresid-smoke/"]},
        )
        self.assertFalse(Path(str(captured["config_path"])).exists())

    def test_submit_rejects_dataset_paths_but_accepts_explicit_output_selectors(self) -> None:
        gateway = BohrCliGateway(
            BohrCliConfig(retry_delay=0, retry_jitter=0),
            environ={},
        )
        base: dict[str, Any] = {
            "name": "worker-001",
            "operation": "bohr.batch.worker",
            "image": "registry.example/chemistry:latest",
            "project_id": "123",
            "machine_type": "c2_m4_cpu",
        }
        with self.assertRaises(InvalidRequestError):
            gateway.submit(BohrJobSpec(**base, parameters={"dataset_paths": ["/bohr/data/v1"]}))

        result = SimpleNamespace(
            returncode=0,
            output='{"ok":true,"data":{"bohrJobId":22}}',
        )
        with patch.object(gateway, "_run_once", return_value=result) as run_once:
            self.assertEqual(
                gateway.submit(
                    BohrJobSpec(**base, parameters={"backward_files": "results/*.tar"})
                ),
                "22",
            )
        args = run_once.call_args.args[0]
        config_path = Path(args[args.index("--config_file") + 1])
        self.assertFalse(config_path.exists())


if __name__ == "__main__":
    unittest.main()


def test_shared_inputs_are_expanded_only_for_upload_and_cleaned(tmp_path: Path) -> None:
    from tricycle_core.asset_store import put_tree
    from tricycle_core.io import write_json

    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "fixed.dat").write_bytes(b"fixed")
    ref = put_tree(assets, tmp_path / "store")
    overlay = tmp_path / "input"
    overlay.mkdir()
    (overlay / "request.json").write_text("{}")
    manifest = tmp_path / "input-assets.json"
    write_json(manifest, {"schema": "tricycle.asset-input/v1", "trees": [ref], "files": {}})
    job = BohrJobSpec(name="compact", operation="test", image="test", command=("true",),
                      project_id="1", machine_type="test", parameters={
                          "input_directory": str(overlay), "input_assets_manifest": str(manifest)})
    gateway = BohrCliGateway(BohrCliConfig(submit_retry_mode="none"), environ={})
    paths = []

    def upload(args: list[str], *, mode: str) -> str:
        directory = Path(args[args.index("--input_directory") + 1])
        paths.append(directory)
        assert directory != overlay
        assert (directory / "fixed.dat").read_bytes() == b"fixed"
        assert (directory / "request.json").read_text() == "{}"
        return '{"bohrJobId":22}'

    with patch.object(gateway, "_run", side_effect=upload):
        assert gateway.submit(job) == "22"
    assert not paths[0].exists()
    assert not (overlay / "fixed.dat").exists()
    with patch.object(gateway, "_run", side_effect=BohrGatewayError("invalid input")):
        import pytest
        with pytest.raises(BohrGatewayError):
            gateway.submit(job)
    assert ref["sha256"]


def test_shared_directory_views_upload_as_independent_writable_files(tmp_path: Path) -> None:
    from tricycle_core.asset_store import externalize_tree, resolve_tree

    root = tmp_path / "worker"
    common = root / "common"
    common.mkdir(parents=True)
    (common / "weights.pt").write_bytes(b"input weights")
    (common / "empty").mkdir()
    ref = externalize_tree(common, tmp_path / "store")
    job = BohrJobSpec(name="shared", operation="test", image="test", command=("true",),
                     project_id="1", machine_type="test",
                     parameters={"input_directory": str(root)})
    gateway = BohrCliGateway(BohrCliConfig(submit_retry_mode="none"), environ={})
    uploads = []

    def upload(args: list[str], *, mode: str) -> str:
        directory = Path(args[args.index("--input_directory") + 1])
        uploads.append(directory)
        assert not (directory / "common").is_symlink()
        assert (directory / "common/empty").is_dir()
        path = directory / "common/weights.pt"
        assert path.read_bytes() == b"input weights"
        path.write_bytes(b"worker modification")
        return '{"bohrJobId":22}'

    with patch.object(gateway, "_run", side_effect=upload):
        assert gateway.submit(job) == "22"
    assert not uploads[0].exists()
    assert (resolve_tree(ref) / "weights.pt").read_bytes() == b"input weights"


def test_reference_only_batch_submits_original_inputs_and_cleans_on_failure(tmp_path: Path) -> None:
    from tricycle_bohrsubmit.lifecycle import job_for_row, read_submit_rows
    from tricycle_bohrsubmit.models import BatchRequest
    from tricycle_bohrsubmit.staging import stage_batch

    script = tmp_path / "task.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    request = BatchRequest.from_mapping({
        "script": str(script), "task_specs": [{"name": "fold-0", "fold": 0}],
        "workers": 1, "project_id": "1", "machine_type": "cpu",
    })
    run = stage_batch(request, run_id="compact", workdir=tmp_path, dry_run=True).run_dir
    assert not list(run.rglob("*.sh"))
    assert not list(run.glob("worker-*/tasks"))
    assert not any(p.is_symlink() for p in run.rglob("*"))
    job = job_for_row(run, read_submit_rows(run)[0])
    assert "input_directory" not in job.parameters
    gateway = BohrCliGateway(BohrCliConfig(submit_retry_mode="none"), environ={})
    uploaded = []

    def upload(args: list[str], *, mode: str) -> str:
        directory = Path(args[args.index("--input_directory") + 1])
        uploaded.append(directory)
        assert (directory / "common/__bohr_task_script.sh").read_bytes() == script.read_bytes()
        assert len(list((directory / "tasks").glob("*.toml"))) == 1
        assert (directory / "run_worker.sh").stat().st_mode & 0o111
        raise BohrGatewayError("upload interrupted")

    import pytest
    with patch.object(gateway, "_run", side_effect=upload), pytest.raises(
        BohrGatewayError, match="upload interrupted"
    ):
        gateway.submit(job)
    assert uploaded and not uploaded[0].exists()
    assert (run / "worker-000/input-assets.json").is_file()


def test_missing_worker_manifest_rejects_before_group_creation(tmp_path: Path) -> None:
    from unittest.mock import Mock

    import pytest
    from tricycle_bohrsubmit.models import BatchRequest
    from tricycle_bohrsubmit.plugin import BohrSubmitPlugin
    from tricycle_bohrsubmit.staging import stage_batch
    from tricycle_core import ExecutionContext
    from tricycle_core.errors import InvalidRequestError

    script = tmp_path / "task.sh"
    script.write_text("exit 0\n")
    request = BatchRequest.from_mapping({
        "script": str(script), "task_specs": [{"name": "task", "fold": 0}],
        "workers": 1, "project_id": "1", "machine_type": "cpu",
    })
    run = stage_batch(request, run_id="missing", workdir=tmp_path, dry_run=True).run_dir
    (run / "worker-000/input-assets.json").unlink()
    gateway = Mock()
    with pytest.raises(InvalidRequestError, match="Shared worker input assets rejected"):
        BohrSubmitPlugin().submit_prepared(run, gateway, ExecutionContext("submit", tmp_path, False))
    assert not gateway.mock_calls
