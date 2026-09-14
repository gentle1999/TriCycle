from __future__ import annotations

import json
import os
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from tricycle_core import IntegrationNotConfiguredError
from tricycle_reaction_db import (
    CalculationLogBatchSpec,
    HttpReactionDatabaseAdapter,
    ReactionBatchImportRequest,
    ReactionDatabaseHttpConfig,
    ReactionDatabaseProjectSpec,
    ReactionFilterRequest,
    RitsDatasetExportRequest,
    RxnResidDatasetExportRequest,
)


class _ApiState:
    def __init__(self) -> None:
        self.project: dict[str, object] | None = None
        self.upload_batch: dict[str, object] | None = None
        self.items: dict[str, dict[str, object]] = {}
        self.requests: list[tuple[str, str, dict[str, list[str]], bytes]] = []
        self.mapped_reaction_id = str(uuid.uuid4())
        self.geometry_id = str(uuid.uuid4())

    def batch_view(self) -> dict[str, object]:
        item_values = list(self.items.values())
        statuses = [str(item.get("status", "")) for item in item_values]
        complete = bool(statuses) and all(
            status in {"staged", "processing", "succeeded"} for status in statuses
        )
        return {
            "id": self.upload_batch["id"] if self.upload_batch else "",
            "project_id": self.project["id"] if self.project else "",
            "status": "completed" if complete else "active",
            "shared_metadata": self.upload_batch.get("shared_metadata", {})
            if self.upload_batch
            else {},
            "manifest_sha256": self.upload_batch.get("manifest_sha256")
            if self.upload_batch
            else None,
            "total_count": len(item_values),
            "total_bytes": sum(int(item.get("size_bytes", 0)) for item in item_values),
            "succeeded_count": sum(status == "succeeded" for status in statuses),
            "failed_count": sum(status == "failed" for status in statuses),
            "cancelled_count": sum(status == "cancelled" for status in statuses),
            "uploading_count": sum(status == "uploading" for status in statuses),
            "staged_count": sum(status == "staged" for status in statuses),
            "processing_count": sum(status == "processing" for status in statuses),
        }


class _ApiHandler(BaseHTTPRequestHandler):
    state: _ApiState

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length)

    def _record(self, body: bytes) -> tuple[str, str, dict[str, list[str]], bytes]:
        parsed = urlsplit(self.path)
        request = (self.command, parsed.path, parse_qs(parsed.query), body)
        self.state.requests.append(request)
        return request

    def _respond(
        self, status: int, payload: object, *, content_type: str = "application/json"
    ) -> None:
        if isinstance(payload, bytes):
            body = payload
        elif content_type == "application/json":
            body = json.dumps(payload).encode("utf-8")
        else:
            body = str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_auth(self) -> bool:
        if self.headers.get("Authorization") != "Bearer test-token":
            self._respond(401, {"detail": "unauthorized"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        request = self._record(b"")
        path = request[1]
        if path == "/api/projects":
            self._respond(200, [self.state.project] if self.state.project else [])
            return
        if path == "/api/upload-batches":
            batches = [self.state.batch_view()] if self.state.upload_batch else []
            self._respond(200, {"items": batches, "total": len(batches), "limit": 100, "offset": 0})
            return
        if path.endswith("/items"):
            items = list(self.state.items.values())
            self._respond(200, {"items": items, "total": len(items), "limit": 200, "offset": 0})
            return
        if path.startswith("/api/upload-batches/"):
            self._respond(200, self.state.batch_view())
            return
        if path == "/api/mapped-reactions":
            self._respond(
                200,
                {
                    "items": [
                        {
                            "id": self.state.mapped_reaction_id,
                            "mapped_reaction_smiles": "[C:1]>>[C:1]",
                        }
                    ],
                    "page": {"total": 1, "limit": 200, "offset": 0, "next_cursor": None},
                },
            )
            return
        if path == f"/api/mapped-reactions/{self.state.mapped_reaction_id}":
            self._respond(
                200,
                {
                    "id": self.state.mapped_reaction_id,
                    "mapped_reaction_smiles": "[C:1]>>[C:1]",
                    "edges": [],
                    "nodes": [
                        {
                            "id": str(uuid.uuid4()),
                            "role": "transition_state",
                            "geometries": [
                                {
                                    "geometry_id": self.state.geometry_id,
                                    "is_primary": True,
                                    "energy_view": {"is_transition_state": True},
                                }
                            ],
                        }
                    ],
                },
            )
            return
        if path == f"/api/depictions/geometry/{self.state.geometry_id}.xyz":
            self._respond(200, b"1\nH 0.0 0.0 0.0\n", content_type="chemical/x-xyz")
            return
        self._respond(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        body = self._body()
        request = self._record(body)
        path = request[1]
        if path == "/api/projects":
            payload = json.loads(body)
            self.state.project = {
                "id": str(uuid.uuid4()),
                **payload,
                "status": "active",
            }
            self._respond(201, self.state.project)
            return
        if path == "/api/upload-batches":
            payload = json.loads(body)
            batch_id = str(uuid.uuid4())
            self.state.upload_batch = {"id": batch_id, **payload}
            self.state.items = {}
            for position, file_spec in enumerate(payload["files"]):
                item = {
                    "id": str(uuid.uuid4()),
                    "client_file_id": file_spec["client_file_id"],
                    "position": position,
                    "original_filename": file_spec["original_filename"],
                    "relative_path": file_spec["relative_path"],
                    "size_bytes": file_spec["size_bytes"],
                    "media_type": file_spec["media_type"],
                    "status": "queued",
                    "error_message": None,
                }
                self.state.items[str(item["client_file_id"])] = item
            self._respond(201, self.state.batch_view())
            return
        prefix = "/api/upload-batches/"
        if prefix in path and "/files/" in path:
            client_file_id = path.rsplit("/", 1)[-1]
            item = self.state.items[client_file_id]
            item["status"] = "staged"
            item["content_seen"] = b"Gaussian" in body
            self._respond(200, item)
            return
        if path == "/api/mapped-reactions/thermodynamics/export.csv":
            self._respond(
                200,
                b"mapped_reaction_smiles,reaction_free_energy,activation_free_energy,reactant_compute_seconds,transition_state_compute_seconds,product_compute_seconds\n[C:1]>>[C:1],1,2,3,4,5\n",
                content_type="text/csv",
            )
            return
        self._respond(404, {"detail": "not found"})


class HttpReactionDatabaseAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._no_proxy = os.environ.get("NO_PROXY")
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        cls.state = _ApiState()
        handler = type("TestApiHandler", (_ApiHandler,), {"state": cls.state})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        if cls._no_proxy is None:
            os.environ.pop("NO_PROXY", None)
        else:
            os.environ["NO_PROXY"] = cls._no_proxy

    def setUp(self) -> None:
        self.state.project = None
        self.state.upload_batch = None
        self.state.items = {}
        self.state.requests.clear()

    def _request(self, root: Path) -> ReactionBatchImportRequest:
        return ReactionBatchImportRequest(
            project=ReactionDatabaseProjectSpec("rits-zero-shot-ts", "RitS zero-shot TS"),
            batch=CalculationLogBatchSpec(
                "bohr-batch-001",
                root,
                metadata={"bohr_job_group_id": "16467504", "bohr_project_id": "991668"},
            ),
            source="bohr.gaussian-direct-ts-freq-calcfc",
            provenance={"workflow": "RitS -> Gaussian", "initial_guess_source": "RitS zero-shot"},
        )

    def _adapter(self) -> HttpReactionDatabaseAdapter:
        return HttpReactionDatabaseAdapter(
            ReactionDatabaseHttpConfig(
                self.base_url,
                "test-token",
                organization_id="organization-001",
                wait_for_completion=True,
                poll_interval_seconds=0.001,
                retry_delay_seconds=0,
            )
        )

    def test_batch_import_creates_project_registers_files_and_is_idempotent(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "downloads" / "worker-000"
            logs.mkdir(parents=True)
            (logs / "a.log").write_text("Gaussian A\n", encoding="utf-8")
            (logs / "b.log").write_text("Gaussian B\n", encoding="utf-8")
            request = self._request(root)
            manifest = request.resolve_batch()

            first = self._adapter().ingest_calculation_logs(request, manifest)
            second = self._adapter().ingest_calculation_logs(request, manifest)

            self.assertEqual(first.status, "succeeded")
            self.assertEqual(first.project_id, second.project_id)
            self.assertEqual(first.accepted_log_count, 2)
            self.assertEqual(first.failed_log_count, 0)
            self.assertEqual(
                sum(
                    path == "/api/projects" and method == "POST"
                    for method, path, _, _ in self.state.requests
                ),
                1,
            )
            self.assertEqual(
                sum(
                    path == "/api/upload-batches" and method == "POST"
                    for method, path, _, _ in self.state.requests
                ),
                1,
            )
            upload_requests = [
                item for item in self.state.requests if item[0] == "POST" and "/files/" in item[1]
            ]
            self.assertEqual(len(upload_requests), 2)
            self.assertEqual(self.state.upload_batch["manifest_sha256"], manifest.manifest_digest)
            self.assertTrue(all(item["content_seen"] for item in self.state.items.values()))

            create_body = next(
                body
                for method, path, _, body in self.state.requests
                if method == "POST" and path == "/api/upload-batches"
            )
            upload_payload = json.loads(create_body)
            self.assertEqual(len(upload_payload["files"]), 2)
            self.assertTrue(all(file["is_gaussian_log"] for file in upload_payload["files"]))

    def test_import_requires_an_explicit_organization_before_network_mutation(self) -> None:
        config = ReactionDatabaseHttpConfig(self.base_url, "test-token")
        adapter = HttpReactionDatabaseAdapter(config)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "downloads"
            logs.mkdir()
            (logs / "one.log").write_text("Gaussian\n", encoding="utf-8")
            request = self._request(root)
            manifest = request.resolve_batch()
            with self.assertRaises(IntegrationNotConfiguredError):
                adapter.ingest_calculation_logs(request, manifest)
        self.assertEqual(self.state.requests, [])

    def test_filter_and_exports_use_documented_api_routes(self) -> None:
        adapter = self._adapter()
        filtered = adapter.filter_cleaned_reactions(ReactionFilterRequest("project-001"))
        self.assertEqual(filtered.selected_count, 1)
        mapped_query = next(
            query
            for method, path, query, _ in self.state.requests
            if method == "GET" and path == "/api/mapped-reactions"
        )
        self.assertEqual(
            mapped_query["reaction_smarts"][0], ReactionFilterRequest("project-001").reaction_smarts
        )

        with TemporaryDirectory() as temp:
            root = Path(temp)
            rxnresid = adapter.export_rxnresid_dataset(
                RxnResidDatasetExportRequest("project-001", root / "rxnresid.csv")
            )
            self.assertEqual(rxnresid.row_count, 1)
            self.assertIn("activation_free_energy", (root / "rxnresid.csv").read_text())

            rits = adapter.export_rits_dataset(
                RitsDatasetExportRequest("project-001", root / "rits.csv")
            )
            self.assertEqual(rits.row_count, 1)
            self.assertIn("ts_structure", (root / "rits.csv").read_text())


if __name__ == "__main__":
    unittest.main()
