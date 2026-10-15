import csv
import os
import tempfile
import tomllib
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from tricycle_bohrsubmit import (
    DEFAULT_CHEMISTRY_IMAGE,
    BatchRequest,
    BohrCliConfig,
    BohrSubmitPlugin,
    DpaBernyRequest,
    InMemoryBohrGateway,
    RescueRequest,
)
from tricycle_bohrsubmit.training_protocol import worker_input
from tricycle_core import ExecutionContext, TaskStatus


class BohrSubmitTests(unittest.TestCase):
    def test_dpa_plan_is_typed_and_uses_shared_image_default(self) -> None:
        plugin = BohrSubmitPlugin()
        plan = plugin.plan(
            "dpa-berny.prepare",
            {
                "xyz_patterns": ["inputs/*.xyz"],
                "workers": 2,
                "model_path": "model.ckpt.pt",
                "project_id": "123",
                "machine_type": "c4_m15_1 * NVIDIA T4",
            },
            ExecutionContext(run_id="plan-run"),
        )
        self.assertEqual(plan.parameters["request"]["image"], DEFAULT_CHEMISTRY_IMAGE)
        self.assertEqual(plan.metadata["engine"], "dpa")

    def test_dpa_prepare_preserves_link1_and_multi_stage_checkpoint_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            xyz = root / "case_ts.xyz"
            xyz.write_text("3\n1,2;2,3\nC 0 0 0\nO 1 0 0\nH 0 1 0\n", encoding="utf-8")
            model = root / "model.ckpt.pt"
            model.write_text("model", encoding="utf-8")
            request = DpaBernyRequest(
                xyz_patterns=(str(xyz),),
                workers=1,
                model_path=model,
                project_id="123",
                machine_type="1 * NVIDIA T4_16g",
                multi_stage_chk=True,
                out_dir=root / "run",
            )
            result = BohrSubmitPlugin().prepare(
                "dpa-berny.prepare",
                request.to_dict(),
                ExecutionContext(run_id="dpa-run"),
            )
            self.assertEqual(result.status, TaskStatus.PLANNED)
            inputs = worker_input(root / "run", "worker-000")
            gjf = next(inputs.glob("*.gjf"))
            content = gjf.read_text(encoding="utf-8")
            self.assertIn("%chk=case_ts.", content)
            self.assertIn("%oldchk=case_ts.", content)
            self.assertIn("--Link1--", content)
            self.assertIn("B 1 2 F", content)
            self.assertIn("B 2 3 F", content)
            with (root / "run" / "batch.toml").open("rb") as handle:
                config = tomllib.load(handle)
            self.assertEqual(config["run"]["workers"], 1)
            self.assertEqual(config["workflow"]["engine"], "dpa")
            self.assertTrue((inputs / "dpa-berny.env").is_file())
            (root / "run/worker-000/work.sh").write_text("exit 99\n")

            rescue = BohrSubmitPlugin().rescue(
                RescueRequest(
                    source_run=root / "run",
                    workers=1,
                    allow_missing_downloads=True,
                    out_dir=root / "rescue",
                ),
                ExecutionContext(run_id="rescue-run"),
            )
            self.assertEqual(rescue.status, TaskStatus.PLANNED)
            with (root / "rescue" / "batch.toml").open("rb") as handle:
                rescue_config = tomllib.load(handle)
            self.assertEqual(rescue_config["workflow"]["engine"], "dpa")
            self.assertFalse((root / "rescue" / "submit-extra-args.txt").exists())
            rescue_worker = (worker_input(root / "rescue", "worker-000") / "work.sh").read_text()
            self.assertIn("DPA requires CUDA; CPU fallback is disabled", rescue_worker)
            self.assertNotIn("exit 99", rescue_worker)

    def test_generic_batch_prepare_and_submit_uses_injected_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = root / "task.json"
            task.write_text("{}\n", encoding="utf-8")
            script = root / "run-one.sh"
            script.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$1\"\n", encoding="utf-8")
            script.chmod(0o755)
            request = BatchRequest(
                task_patterns=(str(task),),
                workers=2,
                script=script,
                project_id="123",
                machine_type="cpu",
                dataset_paths=("dataset-v1",),
                extra_submit_args=("--priority", "normal"),
                out_dir=root / "run",
            )
            plugin = BohrSubmitPlugin()
            plugin.prepare("batch.prepare", request.to_dict(), ExecutionContext(run_id="batch-run"))
            with (root / "run" / "batch.toml").open("rb") as handle:
                config = tomllib.load(handle)
            self.assertEqual(config["bohr"]["dataset_paths"], ["dataset-v1"])
            self.assertEqual(config["bohr"]["extra_submit_args"], ["--priority", "normal"])
            self.assertFalse((root / "run" / "submit-extra-args.txt").exists())
            gateway = InMemoryBohrGateway()
            result = plugin.submit_prepared(
                root / "run",
                gateway,
                ExecutionContext(run_id="submit-run", dry_run=False),
                continue_on_error=False,
            )
            self.assertEqual(result.status, TaskStatus.SUBMITTED)
            with (root / "run" / "submit-results.tsv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(sum(row["status"] == "SUBMITTED" for row in rows), 1)
            self.assertEqual(len(gateway.groups), 1)
            submitted_job = next(iter(gateway.jobs.values()))
            self.assertEqual(submitted_job.parameters["dataset_paths"], ["dataset-v1"])
            self.assertEqual(
                submitted_job.parameters["extra_submit_args"], ["--priority", "normal"]
            )

            repeated = plugin.submit_prepared(
                root / "run",
                gateway,
                ExecutionContext(run_id="submit-retry", dry_run=False),
            )
            self.assertEqual(repeated.status, TaskStatus.SUBMITTED)
            self.assertEqual(repeated.metadata["submitted"], 0)
            self.assertEqual(repeated.metadata["reused"], 1)
            self.assertEqual(len(gateway.jobs), 1)

    def test_bohr_adapter_policy_can_be_loaded_from_toml(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bohr.toml"
            path.write_text(
                "[bohr]\nexecutable = 'bohr-test'\n\n[retry]\nmax_attempts = 3\n",
                encoding="utf-8",
            )
            config = BohrCliConfig.from_toml(path)
            self.assertEqual(config.executable, "bohr-test")
            self.assertEqual(config.retry_max, 3)

    def test_bohr_request_uses_environment_defaults_without_reading_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BOHR_IMAGE": "registry.example/chemistry:env",
                "BOHR_PROJECT_ID": "env-project",
                "BOHR_MACHINE_TYPE": "env-machine",
                "BOHR_ACCESS_KEY": "secret-is-not-used-by-the-request-model",
            },
            clear=False,
        ):
            request = BatchRequest.from_mapping(
                {"task_patterns": ["cases/*.json"], "workers": 1, "script": "run.sh"}
            )

        self.assertEqual(request.image, "registry.example/chemistry:env")
        self.assertEqual(request.project_id, "env-project")
        self.assertEqual(request.machine_type, "env-machine")

    def test_bohr_request_loads_dotenv_defaults_without_loading_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text(
                "BOHR_IMAGE=registry.example/chemistry:dotenv\n"
                "BOHR_PROJECT_ID=dotenv-project\n"
                "BOHR_MACHINE_TYPE=dotenv-machine\n"
                "BOHR_ACCESS_KEY=dotenv-secret\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True), chdir(root):
                request = BatchRequest.from_mapping(
                    {"task_patterns": ["cases/*.json"], "workers": 1, "script": "run.sh"}
                )
                self.assertNotIn("BOHR_ACCESS_KEY", os.environ)

        self.assertEqual(request.image, "registry.example/chemistry:dotenv")
        self.assertEqual(request.project_id, "dotenv-project")
        self.assertEqual(request.machine_type, "dotenv-machine")


if __name__ == "__main__":
    unittest.main()
