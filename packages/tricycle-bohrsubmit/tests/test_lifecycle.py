import csv
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tricycle_bohrsubmit.lifecycle import download_run, status_run
from tricycle_core.errors import InvalidRequestError


class ResolvingGateway:
    def __init__(self) -> None:
        self.resolved: list[tuple[str, str | None]] = []
        self.status_queries: list[str] = []
        self.downloads: list[str] = []

    def resolve_job_id(self, external_id: str, *, job_group_id: str | None = None) -> str:
        self.resolved.append((external_id, job_group_id))
        return {"11": "22"}.get(external_id, external_id)

    def status(self, external_id: str) -> dict[str, str]:
        self.status_queries.append(external_id)
        return {"external_id": external_id, "status": "Finished"}

    def download(self, external_id: str, destination: Path) -> tuple[str, ...]:
        self.downloads.append(external_id)
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / "payload.txt"
        payload.write_text("downloaded\n", encoding="utf-8")
        return (str(payload),)


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
                only_succeeded=True,
                resume=True,
            )

            self.assertEqual(summary.succeeded, 2)
            self.assertEqual(gateway.downloads, ["123", "456"])
            with report.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual([row["status"] for row in rows], ["DOWNLOADED", "DOWNLOADED"])
            self.assertTrue((run_dir / "downloads" / "worker-000-job-123").is_dir())

    def test_legacy_internal_ids_are_resolved_for_status_and_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "batch.env").write_text("job_group_id=456\n", encoding="utf-8")
            (run_dir / "submit-results.tsv").write_text(
                "worker\ttasks\tjob_id\tjob_name\tstatus\tinput_dir\n"
                "worker-000\t1\t11\tworker-000\tSUBMITTED\tinput\n",
                encoding="utf-8",
            )
            gateway = ResolvingGateway()

            status_file, status_summary = status_run(run_dir, gateway)
            self.assertEqual(status_summary.succeeded, 1)
            with status_file.open(encoding="utf-8", newline="") as handle:
                status_row = next(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(status_row["job_id"], "11")
            self.assertEqual(status_row["result"], "SUCCEEDED")
            self.assertEqual(gateway.status_queries, ["22"])

            download_file, download_summary = download_run(
                run_dir,
                gateway,
                only_succeeded=True,
                resume=True,
            )
            self.assertEqual(download_summary.succeeded, 1)
            self.assertEqual(gateway.downloads, ["22"])
            with download_file.open(encoding="utf-8", newline="") as handle:
                download_row = next(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(download_row["status"], "DOWNLOADED")
            self.assertTrue((run_dir / "downloads" / "worker-000-job-11" / "payload.txt").is_file())
            self.assertEqual(gateway.resolved, [("11", "456")])
            self.assertIn(
                "11\t22",
                (run_dir / "bohr-id-map.tsv").read_text(encoding="utf-8"),
            )

    def test_persisted_id_map_is_used_after_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "batch.env").write_text("job_group_id=456\n", encoding="utf-8")
            write_submit_row(run_dir, job_id="11")
            (run_dir / "bohr-id-map.tsv").write_text(
                "internal_job_id\tbohr_id\n11\t22\n",
                encoding="utf-8",
            )

            class NoResolveGateway(ResolvingGateway):
                def resolve_job_id(self, external_id: str, *, job_group_id: str | None = None) -> str:
                    raise AssertionError("persisted id map should avoid a remote resolution")

            gateway = NoResolveGateway()
            status_file, summary = status_run(run_dir, gateway)

            self.assertEqual(summary.succeeded, 1)
            self.assertEqual(gateway.status_queries, ["22"])
            with status_file.open(encoding="utf-8", newline="") as handle:
                self.assertEqual(next(csv.DictReader(handle, delimiter="\t"))["result"], "SUCCEEDED")

    def test_legacy_valid_archive_marker_is_migrated_without_redownload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            write_submit_row(run_dir)
            destination = run_dir / "downloads" / "worker-000-job-123" / "123"
            destination.mkdir(parents=True)
            archive = destination / "out.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("result.txt", "existing result\n")
            (destination / "result.txt").write_text("existing result\n", encoding="utf-8")
            (destination / ".bohr-download-unzipped").touch()
            gateway = ArchiveGateway()

            _, summary = download_run(run_dir, gateway, resume=True)

            self.assertEqual(summary.succeeded, 1)
            self.assertEqual(gateway.downloads, [])
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
