from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from tricycle_core import ExecutionContext, TaskStatus
from tricycle_core.errors import IntegrationNotConfiguredError, InvalidRequestError
from tricycle_rits import (
    InMemoryRitsSshGateway,
    OpenSshRitsGateway,
    RitsSshError,
    RitsSshFinetuneRequest,
    RitsSshPlugin,
)


class RitsSshTests(unittest.TestCase):
    def _request(self, **overrides: object) -> dict[str, object]:
        request: dict[str, object] = {
            "host": "gpu-box.internal",
            "user": "trainer",
            "port": 2202,
            "remote_root": "/mnt/fast/rits-jobs",
            "bundle_dir": "/tmp/rits-bundle",
            "command": ["bash", "run.sh"],
            "container_image": "registry.internal/rits:pt212-cu130",
        }
        request.update(overrides)
        return request

    def test_request_validation_rejects_unsafe_remote_paths(self) -> None:
        with self.assertRaisesRegex(InvalidRequestError, "remote_root"):
            RitsSshFinetuneRequest.from_mapping(
                self._request(remote_root="/mnt/../etc"), operation="submit"
            )
        with self.assertRaisesRegex(InvalidRequestError, "download_paths"):
            RitsSshFinetuneRequest.from_mapping(
                self._request(
                    job_id="run-1",
                    download_paths=["../../etc/passwd"],
                    download_dir="artifacts",
                ),
                operation="download",
            )
        with self.assertRaisesRegex(InvalidRequestError, "command is required"):
            RitsSshFinetuneRequest.from_mapping(
                self._request(command=[]), operation="submit"
            )
        with self.assertRaisesRegex(InvalidRequestError, "identity_file"):
            RitsSshFinetuneRequest.from_mapping(
                self._request(identity_file="~/.ssh/id_ed25519"), operation="submit"
            )

    def test_plan_and_dry_run_do_not_invoke_ssh_gateway(self) -> None:
        component = RitsSshPlugin()
        context = ExecutionContext(run_id="rits-ssh-smoke")
        plan = component.plan("submit", self._request(), context)
        result = component.execute(plan, context)

        self.assertEqual(result.status, TaskStatus.PLANNED)
        self.assertTrue(plan.parameters["ssh_request"]["job_id"].startswith("rits-ssh-smoke-"))
        self.assertEqual(plan.metadata["target"], "trainer@gpu-box.internal")
        self.assertEqual(plan.parameters["ssh_request"]["command"], ["bash", "run.sh"])

    def test_all_ssh_operations_reject_unknown_fields_and_preserve_round_trip(self) -> None:
        for operation in ('submit', 'status', 'logs', 'download', 'terminate'):
            with self.subTest(operation=operation):
                payload = self._request(
                    job_id='job-test', download_paths=['outputs/metrics.csv'], download_dir='outputs',
                )
                request = RitsSshFinetuneRequest.from_mapping(payload, operation=operation)
                self.assertEqual(
                    RitsSshFinetuneRequest.from_mapping(request.to_dict(), operation=operation),
                    request,
                )
                with self.assertRaisesRegex(InvalidRequestError, 'container_gpu'):
                    RitsSshPlugin().plan(
                        operation, {**payload, 'container_gpu': '0'}, ExecutionContext('bad-ssh'),
                    )

    def test_in_memory_gateway_supports_task_lifecycle(self) -> None:
        request = RitsSshFinetuneRequest.from_mapping(
            self._request(job_id="rits-run-1"), operation="submit"
        )
        gateway = InMemoryRitsSshGateway()

        submitted = gateway.submit(request, "rits-run-1")
        status = gateway.status(request, "rits-run-1")
        gateway.log_data["rits-run-1"] = "epoch=1\n"
        log_tail = gateway.logs(request, "rits-run-1")
        terminated = gateway.terminate(request, "rits-run-1")

        self.assertEqual(submitted["state"], "running")
        self.assertEqual(status["job_id"], "rits-run-1")
        self.assertEqual(log_tail, "epoch=1\n")
        self.assertEqual(terminated["state"], "stopped")
        self.assertEqual(gateway.terminated, ["rits-run-1"])

    def test_open_ssh_submit_creates_context_stages_bundle_and_detaches_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "bundle"
            bundle.mkdir()
            (bundle / "run.sh").write_text("#!/usr/bin/env bash\necho train\n", encoding="utf-8")
            request = RitsSshFinetuneRequest.from_mapping(
                self._request(bundle_dir=str(bundle), job_id="rits-run-2"), operation="submit"
            )
            context_created = False

            def successful_command(args: list[str], **_: object) -> CompletedProcess[str]:
                nonlocal context_created

                output = ""
                if args[:3] == ["docker", "context", "ls"]:
                    output = "default\nremote\n" if context_created else "default\n"
                elif args[:3] == ["docker", "context", "create"]:
                    context_created = True
                elif args[:3] == ["docker", "context", "inspect"]:
                    output = "ssh://trainer@gpu-box.internal:2202\n"
                elif args[:3] == ["docker", "--context", "remote"] and args[3] == "info":
                    output = "27.5.1\n"
                elif args[:3] == ["docker", "--context", "remote"] and args[3] == "run":
                    output = "container-123\n"
                return CompletedProcess(args, 0, output, "")

            with patch("tricycle_rits.ssh.subprocess.run", side_effect=successful_command) as run:
                result = OpenSshRitsGateway().submit(request, "rits-run-2")

        self.assertEqual(result["state"], "running")
        self.assertEqual(result["container_id"], "container-123")
        self.assertEqual(result["docker_context"], "remote")
        self.assertEqual(result["docker_server_version"], "27.5.1")
        calls = [call.args[0] for call in run.call_args_list]
        create = next(args for args in calls if args[:3] == ["docker", "context", "create"])
        self.assertEqual(create[3], "remote")
        self.assertIn("host=ssh://trainer@gpu-box.internal:2202", create)
        self.assertTrue(any(args[0] == "ssh" for args in calls))
        self.assertTrue(any(args[0] == "scp" for args in calls))
        docker_run = next(args for args in calls if args[:4] == ["docker", "--context", "remote", "run"])
        self.assertIn("--gpus", docker_run)
        self.assertIn("all", docker_run)
        self.assertIn("WANDB_MODE=disabled", docker_run)
        self.assertIn("registry.internal/rits:pt212-cu130", docker_run)
        image_index = docker_run.index("registry.internal/rits:pt212-cu130")
        self.assertEqual(docker_run[image_index + 1 : image_index + 3], ["/bin/bash", "-c"])

    def test_existing_context_for_another_host_is_not_overwritten(self) -> None:
        request = RitsSshFinetuneRequest.from_mapping(
            self._request(job_id="rits-conflict"), operation="submit"
        )

        def conflicting_context(args: list[str], **_: object) -> CompletedProcess[str]:

            output = "remote\n" if args[:3] == ["docker", "context", "ls"] else "ssh://elsewhere\n"
            return CompletedProcess(args, 0, output, "")

        with (
            patch("tricycle_rits.ssh.subprocess.run", side_effect=conflicting_context),
            self.assertRaisesRegex(RitsSshError, "already points to"),
        ):
            OpenSshRitsGateway().status(request, "rits-conflict")

    def test_status_does_not_create_a_missing_local_docker_context(self) -> None:
        request = RitsSshFinetuneRequest.from_mapping(
            self._request(job_id="rits-no-context"), operation="status"
        )
        calls: list[list[str]] = []

        def context_list(args: list[str], **_: object) -> CompletedProcess[str]:

            calls.append(args)
            return CompletedProcess(args, 0, "default\n", "")

        with (
            patch("tricycle_rits.ssh.subprocess.run", side_effect=context_list),
            self.assertRaisesRegex(IntegrationNotConfiguredError, "not registered"),
        ):
            OpenSshRitsGateway().status(request, "rits-no-context")
        self.assertFalse(any(args[:3] == ["docker", "context", "create"] for args in calls))

    def test_open_ssh_status_logs_and_termination_parse_remote_state(self) -> None:
        request = RitsSshFinetuneRequest.from_mapping(
            self._request(job_id="rits-run-3", log_lines=20), operation="status"
        )
        gateway = OpenSshRitsGateway()
        def successful_command(args: list[str], **_: object) -> CompletedProcess[str]:

            if args[:3] == ["docker", "context", "ls"]:
                output = "remote\n"
            elif args[:3] == ["docker", "context", "inspect"]:
                output = "ssh://trainer@gpu-box.internal:2202\n"
            elif args[:4] == ["docker", "--context", "remote", "container"]:
                output = "running|0|started|\n"
            elif args[:4] == ["docker", "--context", "remote", "logs"]:
                output = "epoch 2/75\n"
            else:
                output = "trainer-rits-run-3\n"
            return CompletedProcess(args, 0, output, "")

        with patch("tricycle_rits.ssh.subprocess.run", side_effect=successful_command):
            status = gateway.status(request, "rits-run-3")
            log_tail = gateway.logs(request, "rits-run-3")
            terminated = gateway.terminate(request, "rits-run-3")

        self.assertEqual(status["state"], "running")
        self.assertEqual(log_tail, "epoch 2/75")
        self.assertEqual(terminated["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
