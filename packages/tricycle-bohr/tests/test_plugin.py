import tempfile
import unittest
from pathlib import Path
from typing import Any

from tricycle_bohr import BohrComputePlugin
from tricycle_core import BohrJobSpec, ExecutionContext, TaskStatus


class RecordingBohrGateway:
    """A deterministic gateway that records the three Bohr task operations."""

    def __init__(self) -> None:
        self.submitted: list[BohrJobSpec] = []
        self.status_queries: list[str] = []
        self.downloads: list[tuple[str, Path]] = []

    def submit(self, job: BohrJobSpec) -> str:
        self.submitted.append(job)
        return "bohr-job-001"

    def status(self, external_id: str) -> dict[str, Any]:
        self.status_queries.append(external_id)
        return {"external_id": external_id, "status": "RUNNING", "phase": "compute"}

    def download(self, external_id: str, destination: Path) -> tuple[str, ...]:
        self.downloads.append((external_id, destination))
        destination.mkdir(parents=True, exist_ok=True)
        artifact = destination / "results.out"
        artifact.write_text("completed\n", encoding="utf-8")
        return (str(artifact),)


class BohrComputePluginTests(unittest.TestCase):
    def test_task_push_uses_injected_gateway(self) -> None:
        plugin = BohrComputePlugin()
        gateway = RecordingBohrGateway()
        context = ExecutionContext(run_id="submit-run", dry_run=False)
        plan = plugin.plan(
            "compute",
            {
                "job_name": "water",
                "operation": "single-point",
                "image": "registry.example/chemistry:latest",
                "command": ["run-water"],
                "project_id": "project-001",
                "machine_type": "cpu",
            },
            context,
        )

        result = plugin.submit(plan, gateway, context)

        self.assertEqual(result.status, TaskStatus.SUBMITTED)
        self.assertEqual(result.external_id, "bohr-job-001")
        self.assertEqual(len(gateway.submitted), 1)
        self.assertEqual(gateway.submitted[0].name, "water")
        self.assertEqual(gateway.submitted[0].project_id, "project-001")

    def test_task_status_queries_injected_gateway(self) -> None:
        plugin = BohrComputePlugin()
        gateway = RecordingBohrGateway()
        context = ExecutionContext(run_id="status-run", dry_run=False)
        plan = plugin.plan("status", {"external_id": "bohr-job-001"}, context)

        result = plugin.status(plan, gateway, context)

        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.external_id, "bohr-job-001")
        self.assertEqual(result.metadata["status"], "RUNNING")
        self.assertEqual(gateway.status_queries, ["bohr-job-001"])

    def test_result_download_uses_requested_destination(self) -> None:
        plugin = BohrComputePlugin()
        gateway = RecordingBohrGateway()
        context = ExecutionContext(run_id="download-run", dry_run=False)
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "artifacts"
            plan = plugin.plan(
                "download",
                {"external_id": "bohr-job-001", "destination": str(destination)},
                context,
            )

            result = plugin.download(plan, gateway, context)

            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertEqual(result.external_id, "bohr-job-001")
            self.assertEqual(result.metadata["locations"], [str(destination / "results.out")])
            self.assertEqual(gateway.downloads, [("bohr-job-001", destination)])
            self.assertTrue((destination / "results.out").is_file())

    def test_default_dry_run_does_not_call_bohr_gateway(self) -> None:
        plugin = BohrComputePlugin()
        gateway = RecordingBohrGateway()
        context = ExecutionContext(run_id="dry-run")
        plan = plugin.plan(
            "compute",
            {
                "job_name": "water",
                "operation": "single-point",
                "image": "registry.example/chemistry:latest",
                "command": ["run-water"],
            },
            context,
        )

        result = plugin.submit(plan, gateway, context)

        self.assertEqual(result.status, TaskStatus.PLANNED)
        self.assertEqual(gateway.submitted, [])


if __name__ == "__main__":
    unittest.main()
