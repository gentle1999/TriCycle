import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
            environ={"ACCESS_KEY": "legacy-test-key"},
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
        self.assertIn("--output", args)
        self.assertEqual(args[args.index("--output") + 1], "json")
        self.assertNotIn("--backward_files", args)
        self.assertNotIn("--dataset_path", args)
        self.assertEqual(gateway.environ["BOHR_ACCESS_KEY"], "legacy-test-key")

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

    def test_resolve_job_id_maps_legacy_internal_id_from_current_group_listing(self) -> None:
        gateway = BohrCliGateway(
            BohrCliConfig(retry_delay=0, retry_jitter=0),
            environ={},
        )
        result = SimpleNamespace(
            returncode=0,
            output=(
                '{"ok":true,"data":['
                '{"id":11,"bohrId":22,"phase":"failed"},'
                '{"id":12,"bohrId":23,"phase":"finished"}'
                ']}'
            ),
        )
        with patch.object(gateway, "_run_once", return_value=result) as run_once:
            self.assertEqual(gateway.resolve_job_id("11", job_group_id="456"), "22")
            self.assertEqual(gateway.resolve_job_id("23", job_group_id="456"), "23")

        self.assertEqual(run_once.call_count, 1)
        self.assertEqual(
            run_once.call_args.args[0],
            ["job", "describe", "--job_group_id", "456", "--output", "json"],
        )

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
        self.assertFalse(Path(captured["config_path"]).exists())

    def test_submit_rejects_dataset_paths_but_accepts_explicit_output_selectors(self) -> None:
        gateway = BohrCliGateway(
            BohrCliConfig(retry_delay=0, retry_jitter=0),
            environ={},
        )
        base = {
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
