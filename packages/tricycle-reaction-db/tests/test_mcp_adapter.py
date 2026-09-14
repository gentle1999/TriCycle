from __future__ import annotations

import base64
import json
import os
import threading
import unittest
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from tricycle_core import IntegrationNotConfiguredError
from tricycle_reaction_db import (
    CalculationLogBatchSpec,
    McpReactionDatabaseAdapter,
    ReactionBatchImportRequest,
    ReactionDatabaseMcpConfig,
    ReactionDatabaseMcpError,
    ReactionDatabaseProjectSpec,
    ReactionFilterRequest,
    RitsDatasetExportRequest,
    RxnResidDatasetExportRequest,
    validate_export_receipt,
)


class _McpState:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.project_id = "project-001"
        self.include_upload_tool = True
        self.fail_upload_filenames: set[str] = set()


class _McpHandler(BaseHTTPRequestHandler):
    state: _McpState

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _respond(self, payload: object, *, status: int = 200) -> None:
        body = b"event: message\ndata: " + json.dumps(payload).encode() + b"\n\n"
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        self.state.requests.append(body)
        method = body.get("method")
        request_id = body.get("id")
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "fake-db", "version": "1"},
            }
            self._respond({"jsonrpc": "2.0", "id": request_id, "result": result})
            return
        if method == "tools/list":
            names = {
                "compose_query",
                "create_project",
            }
            if self.state.include_upload_tool:
                names.add("upload_calculation_log")
            self._respond(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": [{"name": name} for name in sorted(names)]},
                }
            )
            return
        if method != "tools/call":
            self._respond(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "unknown method"},
                }
            )
            return

        params = body["params"]
        assert isinstance(params, dict)
        name = params["name"]
        arguments = params.get("arguments", {})
        assert isinstance(arguments, dict)
        if self.headers.get("Authorization") != "Bearer test-token":
            self._respond(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": "unauthorized"}],
                    },
                }
            )
            return
        if name == "create_project":
            data: object = {"id": self.state.project_id}
        elif name == "upload_calculation_log":
            filename = arguments["filename"]
            assert isinstance(filename, str)
            if filename in self.state.fail_upload_filenames:
                self._respond(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": "file rejected"}],
                        },
                    }
                )
                return
            content_base64 = arguments["content_base64"]
            assert isinstance(content_base64, str)
            payload = base64.b64decode(content_base64, validate=True)
            expected_size = arguments["expected_size_bytes"]
            expected_sha256 = arguments["expected_sha256"]
            assert expected_size == len(payload)
            assert expected_sha256 == sha256(payload).hexdigest()
            assert arguments["media_type"] == "text/plain"
            assert isinstance(arguments["relative_path"], str)
            data = {
                "artifact_id": f"artifact-{filename}",
                "storage_status": "available",
                "ingestion_status": "succeeded",
                "inferred_reaction_count": 1 if filename == "a.log" else 0,
                "inferences": [],
            }
        elif name == "compose_query":
            query = str(arguments["query"])
            if "list_mapped_reactions" in query:
                data = {
                    "MappedReactionQueryService": {
                        "list_mapped_reactions": {
                            "items": [
                                {
                                    "id": "mapped-001",
                                    "mapped_reaction_smiles": "[C:1]>>[C:1]",
                                }
                            ],
                            "page": {"total": 1, "limit": 200, "offset": 0},
                        }
                    }
                }
            elif "get_mapped_reaction(" in query:
                data = {
                    "MappedReactionQueryService": {
                        "get_mapped_reaction": {
                            "id": "mapped-001",
                            "mapped_reaction_smiles": "[C:1]>>[C:1]",
                            "nodes": [
                                {
                                    "id": "node-ts",
                                    "role": "transition_state",
                                    "geometries": [
                                        {"geometry_id": "geometry-ts", "is_primary": True}
                                    ],
                                }
                            ],
                            "edges": [{"transition_state_node_id": "node-ts"}],
                        }
                    }
                }
            elif "get_geometry(" in query:
                data = {
                    "GeometryQueryService": {
                        "get_geometry": {
                            "id": "geometry-ts",
                            "is_transition_state": True,
                            "coordinates": [
                                {
                                    "atom_index": 0,
                                    "element": "C",
                                    "x_angstrom": 0.0,
                                    "y_angstrom": 1.0,
                                    "z_angstrom": 2.0,
                                }
                            ],
                        }
                    }
                }
            elif "get_mapped_reaction_thermodynamics" in query:
                data = {
                    "ReactionEnergyQueryService": {
                        "get_mapped_reaction_thermodynamics": {
                            "mapped_reaction_id": "mapped-001",
                            "profiles": [
                                {
                                    "reaction": {"gibbs_free_energy_kcal_mol": 1.0},
                                    "activation": {"gibbs_free_energy_kcal_mol": 2.0},
                                    "reactants_running_time_seconds": 3.0,
                                    "transition_state_running_time_seconds": 4.0,
                                    "products_running_time_seconds": 5.0,
                                }
                            ],
                        }
                    }
                }
            else:
                data = {}
        else:
            data = {}
        structured_content = {"data": data, "errors": []} if name == "compose_query" else {
            "success": True,
            "data": data,
        }
        self._respond(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "structuredContent": structured_content,
                    "content": [{"type": "text", "text": json.dumps(data)}],
                    "isError": False,
                },
            }
        )


class McpReactionDatabaseAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._no_proxy = os.environ.get("NO_PROXY")
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        cls.state = _McpState()
        handler = type("TestMcpHandler", (_McpHandler,), {"state": cls.state})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/mcp/"

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
        self.state.requests.clear()
        self.state.include_upload_tool = True
        self.state.fail_upload_filenames.clear()

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

    def _adapter(self, **kwargs: object) -> McpReactionDatabaseAdapter:
        return McpReactionDatabaseAdapter(
            ReactionDatabaseMcpConfig(
                self.base_url,
                "test-token",
                organization_id="organization-001",
                **kwargs,
            )
        )

    def test_import_uploads_each_file_bytes_through_mcp(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "downloads" / "worker-000"
            logs.mkdir(parents=True)
            (logs / "a.log").write_text("Gaussian A\n", encoding="utf-8")
            (logs / "b.log").write_text("Gaussian B\n", encoding="utf-8")
            request = self._request(root)

            receipt = self._adapter().ingest_calculation_logs(request, request.resolve_batch())

        self.assertEqual(receipt.status, "succeeded")
        self.assertEqual(receipt.project_id, self.state.project_id)
        self.assertEqual(receipt.accepted_log_count, 2)
        self.assertEqual(receipt.cleaned_reaction_count, 1)
        methods = [str(item.get("method")) for item in self.state.requests]
        self.assertEqual(
            methods[:4], ["initialize", "notifications/initialized", "tools/list", "tools/call"]
        )
        tool_calls = [
            item["params"]["name"]
            for item in self.state.requests
            if item.get("method") == "tools/call"
        ]
        self.assertEqual(
            tool_calls,
            [
                "create_project",
                "upload_calculation_log",
                "upload_calculation_log",
            ],
        )
        self.assertFalse(any("staged_file_path" in json.dumps(item) for item in self.state.requests))
        upload_calls = [
            item
            for item in self.state.requests
            if item.get("method") == "tools/call"
            and item["params"]["name"] == "upload_calculation_log"
        ]
        self.assertEqual(len(upload_calls), 2)
        first_upload = upload_calls[0]
        arguments = first_upload["params"]["arguments"]
        assert isinstance(arguments, dict)
        self.assertEqual(arguments["filename"], "a.log")
        self.assertEqual(arguments["relative_path"], "downloads/worker-000/a.log")
        self.assertEqual(
            base64.b64decode(arguments["content_base64"], validate=True),
            b"Gaussian A\n",
        )
        self.assertEqual(receipt.metadata["file_transfer"], "base64-per-file")
        self.assertNotIn("staging_root", receipt.metadata)
        create = next(
            item
            for item in self.state.requests
            if item.get("method") == "tools/call"
            and item["params"]["name"] == "create_project"
        )
        create_arguments = create["params"]["arguments"]
        assert isinstance(create_arguments, dict)
        self.assertEqual(
            create_arguments["data_source"]["file_transfer"],
            "mcp.upload_calculation_log.base64",
        )

    def test_import_requires_organization_before_network_mutation(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "downloads"
            logs.mkdir()
            (logs / "one.log").write_text("Gaussian\n", encoding="utf-8")
            request = self._request(root)
            manifest = request.resolve_batch()

            with self.assertRaises(IntegrationNotConfiguredError):
                McpReactionDatabaseAdapter(
                    ReactionDatabaseMcpConfig(self.base_url, "test-token")
                ).ingest_calculation_logs(request, manifest)
            self.assertEqual(self.state.requests, [])

    def test_import_requires_upload_tool_before_project_creation(self) -> None:
        self.state.include_upload_tool = False
        with TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "downloads"
            logs.mkdir()
            (logs / "one.log").write_text("Gaussian\n", encoding="utf-8")
            request = self._request(root)

            with self.assertRaisesRegex(ReactionDatabaseMcpError, "upload_calculation_log"):
                self._adapter().ingest_calculation_logs(request, request.resolve_batch())

        tool_calls = [
            item["params"]["name"]
            for item in self.state.requests
            if item.get("method") == "tools/call"
        ]
        self.assertEqual(tool_calls, [])

    def test_import_continues_after_one_file_upload_error(self) -> None:
        self.state.fail_upload_filenames.add("b.log")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "downloads"
            logs.mkdir()
            (logs / "a.log").write_text("Gaussian A\n", encoding="utf-8")
            (logs / "b.log").write_text("Gaussian B\n", encoding="utf-8")
            request = self._request(root)

            receipt = self._adapter().ingest_calculation_logs(request, request.resolve_batch())

        self.assertEqual(receipt.status, "failed")
        self.assertEqual(receipt.accepted_log_count, 1)
        self.assertEqual(receipt.failed_log_count, 1)
        self.assertEqual(receipt.metadata["uploaded_count"], 1)
        self.assertEqual(receipt.metadata["error_count"], 1)
        uploaded_names = [
            item["params"]["arguments"]["filename"]
            for item in self.state.requests
            if item.get("method") == "tools/call"
            and item["params"]["name"] == "upload_calculation_log"
        ]
        self.assertEqual(uploaded_names, ["a.log", "b.log"])

    def test_filter_and_exports_use_mcp_compose_query(self) -> None:
        adapter = self._adapter()
        filtered = adapter.filter_cleaned_reactions(ReactionFilterRequest("project-001"))
        self.assertEqual(filtered.selected_count, 1)
        self.assertEqual(filtered.metadata["transport"], "mcp")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            rits = adapter.export_rits_dataset(
                RitsDatasetExportRequest("project-001", root / "rits.csv")
            )
            rxnresid = adapter.export_rxnresid_dataset(
                RxnResidDatasetExportRequest("project-001", root / "rxnresid.csv")
            )
            self.assertEqual(rits.row_count, 1)
            self.assertIn("geometry_id=geometry-ts", (root / "rits.csv").read_text())
            self.assertEqual(rxnresid.row_count, 1)
            self.assertIn("[C:1]>>[C:1],1.0,2.0,3.0,4.0,5.0", (root / "rxnresid.csv").read_text())

        validate_export_receipt(
            RitsDatasetExportRequest("project-001", Path("rits.csv")), rits
        )
        validate_export_receipt(
            RxnResidDatasetExportRequest("project-001", Path("rxnresid.csv")), rxnresid
        )
        self.assertTrue(
            all(
                str(item.get("method")) != "/api/projects"
                for item in self.state.requests
            )
        )
        compose_queries = [
            item["params"]["arguments"]["query"]
            for item in self.state.requests
            if item.get("method") == "tools/call"
            and item["params"]["name"] == "compose_query"
        ]
        self.assertTrue(compose_queries)
        self.assertTrue(all("reaction_smarts" in query for query in compose_queries if "list_mapped_reactions" in query))


if __name__ == "__main__":
    unittest.main()
