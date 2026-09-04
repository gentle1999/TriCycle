from __future__ import annotations

import io
import json
import os
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tricycle_agent.bootstrap import create_capability_catalog
from tricycle_agent.cli import main
from tricycle_agent.execution import CapabilityDispatcher
from tricycle_agent.run_state import record_result, resume_request
from tricycle_core import (
    ExecutionContext,
    InvalidRequestError,
    RunStore,
    SideEffectLevel,
    TaskResult,
    TaskStatus,
)


class CapabilityRuntimeTests(unittest.TestCase):
    def test_catalog_exposes_independent_capabilities(self) -> None:
        catalog = create_capability_catalog()
        expected = {
            "reactionopt.generate",
            "candidate.select",
            "bohr.batch.prepare",
            "bohr.batch.submit",
            "bohr.job.status",
            "bohr.job.download",
            "dpa-berny.rescue",
            "reaction-db.ingest",
            "sar.train",
            "sar.infer",
            "sar.finetune",
            "rits.train",
            "rits.infer",
            "rits.finetune",
        }
        self.assertTrue(
            expected.issubset({manifest.capability_id for manifest in catalog.manifests()})
        )
        self.assertEqual(
            catalog.get("bohr.batch.prepare").manifest.side_effect,
            SideEffectLevel.LOCAL_WRITE,
        )
        catalog.validate(
            "reactionopt.generate",
            {"reaction_id": "rxn-1", "reaction_smiles": "C>>C"},
            ExecutionContext(run_id="validate-1"),
        )
        for manifest in catalog.manifests():
            skill = Path(".agents/skills") / manifest.capability_id.replace(".", "-") / "SKILL.md"
            self.assertTrue(skill.is_file(), f"missing Skill for {manifest.capability_id}")
            self.assertIn(
                f"name: {manifest.capability_id.replace('.', '-')}",
                skill.read_text(encoding="utf-8"),
            )

    def test_run_persists_toml_request_and_state(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            request = root / "reaction.toml"
            request.write_text(
                'reaction_id = "rxn-1"\nreaction_smiles = "[C:1]=[C:2]>>[C:1][C:2]"\n',
                encoding="utf-8",
            )
            run_dir = root / "runs" / "rxn-1"
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "run",
                            "reactionopt.generate",
                            str(request),
                            "--run-id",
                            "rxn-1",
                            "--run-dir",
                            str(run_dir),
                        ]
                    ),
                    0,
                )
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "succeeded")
            self.assertTrue((run_dir / "request.toml").is_file())
            self.assertTrue((run_dir / "run.toml").is_file())

            state = tomllib.loads((run_dir / "run.toml").read_text(encoding="utf-8"))
            self.assertEqual(state["schema"], "tricycle.run/v1")
            self.assertEqual(state["run"]["status"], "succeeded")
            self.assertEqual(state["resume"]["next_action"], "")

            inspected = io.StringIO()
            with redirect_stdout(inspected):
                self.assertEqual(main(["inspect", "--run-dir", str(run_dir)]), 0)
            self.assertEqual(json.loads(inspected.getvalue())["run"]["id"], "rxn-1")

    def test_prepare_creates_state_and_dry_run_resume_is_non_mutating(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            task = root / "task.json"
            task.write_text("{}\n", encoding="utf-8")
            script = root / "run.sh"
            script.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$1\"\n", encoding="utf-8")
            script.chmod(0o755)
            request = root / "batch.toml"
            run_dir = root / "run"
            request.write_text(
                f'task_patterns = ["{task}"]\n'
                "workers = 1\n"
                f'script = "{script}"\n'
                'project_id = "project-1"\n'
                'machine_type = "cpu"\n'
                "continue_on_error = true\n"
                "only_succeeded = true\n"
                "interval = 0.25\n"
                f'out_dir = "{run_dir}"\n',
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "run",
                        "bohr.batch.prepare",
                        str(request),
                        "--run-id",
                        "batch-1",
                    ]
                )
            state = RunStore.open(run_dir).read()
            self.assertEqual(state["run"]["phase"], "local")
            self.assertEqual(state["resume"]["next_action"], "bohr.batch.submit")
            action, resumed_request = resume_request(RunStore.open(run_dir))
            self.assertEqual(action, "bohr.batch.submit")
            self.assertTrue(resumed_request["continue_on_error"])
            self.assertTrue(resumed_request["only_succeeded"])
            self.assertEqual(resumed_request["interval"], 0.25)

            with redirect_stdout(io.StringIO()):
                main(["resume", "--run-dir", str(run_dir)])
            resumed = RunStore.open(run_dir).read()
            self.assertEqual(resumed["execution"]["dry_run"], True)
            self.assertEqual(resumed["resume"]["next_action"], "bohr.batch.submit")
            self.assertFalse((run_dir / "job-group-id").exists())

    def test_remote_job_state_follows_adapter_status(self) -> None:
        class FakeBohrGateway:
            status_value = "running"

            def submit(self, job: object) -> str:
                del job
                return "job-1"

            def status(self, external_id: str) -> dict[str, str]:
                return {"external_id": external_id, "status": self.status_value}

            def download(self, external_id: str, destination: Path) -> tuple[str, ...]:
                del external_id, destination
                return ()

        request = {
            "job_name": "water",
            "operation": "reaction.single-point",
            "image": "registry.example/chemistry:latest",
            "command": ["run-water"],
            "project_id": "project-1",
            "machine_type": "cpu",
        }
        gateway = FakeBohrGateway()
        dispatcher = CapabilityDispatcher(create_capability_catalog(), bohr_gateway=gateway)
        context = ExecutionContext(run_id="remote-1", workdir=Path("."), dry_run=False)

        with TemporaryDirectory() as temp:
            store = RunStore.create(
                Path(temp),
                run_id="remote-1",
                capability_id="bohr.compute.submit",
                dry_run=False,
            )
            submitted = dispatcher.execute("bohr.compute.submit", request, context)
            self.assertEqual(submitted.status, TaskStatus.SUBMITTED)
            record_result(store, "bohr.compute.submit", submitted)
            state = store.read()
            self.assertEqual(state["resume"]["next_action"], "bohr.job.status")
            self.assertEqual(state["remote"]["job_id"], "job-1")

            running = dispatcher.execute("bohr.job.status", {"external_id": "job-1"}, context)
            record_result(store, "bohr.job.status", running)
            state = store.read()
            self.assertEqual(state["run"]["status"], "running")
            self.assertEqual(state["resume"]["next_action"], "bohr.job.status")

            gateway.status_value = "completed"
            completed = dispatcher.execute("bohr.job.status", {"external_id": "job-1"}, context)
            record_result(store, "bohr.job.status", completed)
            state = store.read()
            self.assertEqual(state["run"]["status"], "succeeded")
            self.assertEqual(state["resume"]["next_action"], "bohr.job.download")

    def test_destructive_capability_requires_confirmation_before_adapter_access(self) -> None:
        dispatcher = CapabilityDispatcher(create_capability_catalog())
        with self.assertRaises(InvalidRequestError):
            dispatcher.execute(
                "bohr.batch.terminate",
                {"run_dir": "runs/not-a-real-run"},
                ExecutionContext(run_id="terminate-1", dry_run=False),
            )
        preview = dispatcher.execute(
            "bohr.batch.terminate",
            {"run_dir": "runs/not-a-real-run"},
            ExecutionContext(run_id="terminate-preview"),
        )
        self.assertEqual(preview.status, TaskStatus.PLANNED)

    def test_dry_run_does_not_construct_bohr_adapter_or_persist_credentials(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            request = root / "job.toml"
            request.write_text('external_id = "job-1"\n', encoding="utf-8")
            run_dir = root / "run"
            with (
                patch.dict(os.environ, {"BOHR_ACCESS_KEY": "secret-value"}),
                patch.object(
                    CapabilityDispatcher,
                    "with_bohr_cli",
                    side_effect=AssertionError("dry-run must not construct a Bohr adapter"),
                ),
                redirect_stdout(io.StringIO()),
            ):
                main(
                    [
                        "run",
                        "bohr.job.status",
                        str(request),
                        "--run-id",
                        "job-1",
                        "--run-dir",
                        str(run_dir),
                    ]
                )
            self.assertNotIn("secret-value", (run_dir / "run.toml").read_text(encoding="utf-8"))

    def test_live_planning_does_not_construct_a_bohr_adapter(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            request = root / "job.toml"
            request.write_text(
                'job_name = "water"\n'
                'operation = "reaction.single-point"\n'
                'image = "registry.example/chemistry:latest"\n'
                'command = ["run-water"]\n',
                encoding="utf-8",
            )
            with (
                patch.object(
                    CapabilityDispatcher,
                    "with_bohr_cli",
                    side_effect=AssertionError("planning must not construct a Bohr adapter"),
                ),
                redirect_stdout(io.StringIO()),
            ):
                main(
                    [
                        "run",
                        "bohr.compute.plan",
                        str(request),
                        "--run-id",
                        "plan-live-1",
                        "--run-dir",
                        str(root / "run"),
                        "--live",
                    ]
                )

    def test_cli_rejects_non_toml_request_paths(self) -> None:
        with TemporaryDirectory() as temp:
            request = Path(temp) / "request.json"
            request.write_text('reaction_id = "rxn-1"\n', encoding="utf-8")
            with self.assertRaises(SystemExit):
                main(["plan", "reactionopt.generate", str(request)])

    def test_retryable_failure_keeps_next_action(self) -> None:
        with TemporaryDirectory() as temp:
            store = RunStore.create(
                Path(temp),
                run_id="retry-1",
                capability_id="bohr.batch.submit",
                dry_run=False,
            )
            failed = TaskResult(
                task_id="retry-1/submit",
                plugin_id="tricycle.bohr.submit",
                status=TaskStatus.FAILED,
                message="temporary gateway failure",
            )
            record_result(store, "bohr.batch.submit", failed)
            state = store.read()
            self.assertEqual(state["run"]["status"], "failed")
            self.assertEqual(state["resume"]["next_action"], "bohr.batch.submit")


if __name__ == "__main__":
    unittest.main()
