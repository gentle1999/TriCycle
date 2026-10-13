import csv
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tricycle_bohrsubmit.gateway import InMemoryBohrGateway
from tricycle_bohrsubmit.lifecycle import download_run, status_run, terminate_run
from tricycle_core import BohrJobSpec
from tricycle_core.errors import InvalidRequestError


class ArchiveGateway:
    def __init__(self) -> None:
        self.downloads: list[str] = []

    def download(self, external_id: str, destination: Path) -> tuple[str, ...]:
        self.downloads.append(external_id)
        archive_dir = destination / external_id
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive = archive_dir / "out.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("result.txt", "fresh result\n")
        return (str(archive),)


def write_submit_row(run_dir: Path, job_id: str = "123") -> None:
    (run_dir / "submit-results.tsv").write_text(
        "worker\ttasks\tjob_id\tjob_name\tstatus\tinput_dir\n"
        f"worker-000\t1\t{job_id}\tworker-000\tSUBMITTED\tinput\n",
        encoding="utf-8",
    )


class BatchLifecycleTests(unittest.TestCase):
    def test_internal_id_migration_record_is_rejected_before_remote_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            write_submit_row(run, "11")
            (run / "bohr-id-map.tsv").write_text("internal_job_id\tbohr_id\n11\t22\n")
            gateway = ArchiveGateway()
            with self.assertRaisesRegex(InvalidRequestError, "Internal-ID migration"):
                download_run(run, gateway)
            self.assertEqual(gateway.downloads, [])

    def test_opaque_gateway_ids_round_trip_through_download_and_termination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            gateway = InMemoryBohrGateway()
            job_id = gateway.submit(
                BohrJobSpec(name="test", operation="test", image="test", command=("true",))
            )
            write_submit_row(run_dir, job_id)

            report, summary = download_run(run_dir, gateway)
            self.assertEqual(summary.succeeded, 1)
            with report.open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(row["job_id"], job_id)
            destination = Path(row["download_dir"])
            self.assertEqual(destination.parent, run_dir / "downloads")
            self.assertTrue((destination / "memory-job.json").is_file())
            _, resumed = download_run(run_dir, gateway, resume=True)
            self.assertEqual(resumed.succeeded, 1)

            status_run(run_dir, gateway)
            report = terminate_run(run_dir, gateway, confirm=True)
            with report.open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(gateway.terminated, [job_id])
            output = Path(row["output_file"])
            self.assertEqual(output.parent, run_dir / "terminate-outputs")
            self.assertIn(job_id, output.read_text())

    def test_failed_worker_is_downloaded_for_partial_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "submit-results.tsv").write_text(
                "worker\ttasks\tjob_id\tjob_name\tstatus\tinput_dir\n"
                "worker-000\t1\t123\tworker-000\tSUBMITTED\tinput\n"
                "worker-001\t1\t456\tworker-001\tSUBMITTED\tinput\n",
                encoding="utf-8",
            )
            (run_dir / "status.tsv").write_text(
                "worker\ttasks\tjob_id\tjob_name\tsubmit_status\tjob_status\t"
                "terminal\tresult\tinput_dir\tchecked_at\n"
                "worker-000\t1\t123\tworker-000\tSUBMITTED\tFailed\tyes\tFAILED\tinput\t\n"
                "worker-001\t1\t456\tworker-001\tSUBMITTED\tFinished\tyes\tSUCCEEDED\tinput\t\n",
                encoding="utf-8",
            )
            gateway = ArchiveGateway()

            report, summary = download_run(
                run_dir,
                gateway,
                resume=True,
            )

            self.assertEqual(summary.succeeded, 2)
            self.assertEqual(gateway.downloads, ["123", "456"])
            with report.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual([row["status"] for row in rows], ["DOWNLOADED", "DOWNLOADED"])
            self.assertTrue((run_dir / "downloads" / "worker-000-job-123").is_dir())

    def test_empty_archive_marker_requires_validating_and_extracting_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            write_submit_row(run_dir)
            destination = run_dir / "downloads" / "worker-000-job-123" / "123"
            destination.mkdir(parents=True)
            archive = destination / "out.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("result.txt", "existing result\n")
            (destination / "result.txt").write_text("stale extraction\n", encoding="utf-8")
            (destination / ".bohr-download-unzipped").touch()
            gateway = ArchiveGateway()

            _, summary = download_run(run_dir, gateway, resume=True)

            self.assertEqual(summary.succeeded, 1)
            self.assertEqual(gateway.downloads, [])
            self.assertEqual((destination / "result.txt").read_text(), "existing result\n")
            marker = destination / ".bohr-download-unzipped"
            state = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(state["version"], 1)
            self.assertTrue(state["archive_sha256"])
            manifest = (
                run_dir / "downloads" / "worker-000-job-123" / "download-manifest.txt"
            )
            self.assertIn("123/out.zip", manifest.read_text(encoding="utf-8"))

    def test_resume_repairs_corrupt_download_in_a_staging_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            write_submit_row(run_dir)
            destination = run_dir / "downloads" / "worker-000-job-123"
            destination.mkdir(parents=True)
            corrupt_archive = destination / "out.zip"
            corrupt_archive.write_bytes(b"truncated archive")
            gateway = ArchiveGateway()

            _, summary = download_run(run_dir, gateway, resume=True)

            self.assertEqual(summary.succeeded, 1)
            self.assertEqual(gateway.downloads, ["123"])
            self.assertTrue((destination / "123" / "result.txt").is_file())
            backups = sorted(
                (run_dir / "downloads").glob("worker-000-job-123.replaced-*")
            )
            self.assertEqual(len(backups), 1)
            self.assertEqual(
                (backups[0] / "out.zip").read_bytes(), b"truncated archive"
            )
            manifest = destination / "download-manifest.txt"
            self.assertIn("123/result.txt", manifest.read_text(encoding="utf-8"))

    def test_incomplete_existing_download_requires_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            write_submit_row(run_dir)
            destination = run_dir / "downloads" / "worker-000-job-123"
            destination.mkdir(parents=True)
            (destination / "out.zip").write_bytes(b"truncated archive")
            gateway = ArchiveGateway()

            with self.assertRaisesRegex(InvalidRequestError, "resume=True"):
                download_run(run_dir, gateway, resume=False)

            self.assertEqual(gateway.downloads, [])
            report = run_dir / "downloads" / "download-results.tsv"
            self.assertIn("FAILED", report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
