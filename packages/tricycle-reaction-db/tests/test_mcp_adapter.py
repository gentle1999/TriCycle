from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from tricycle_core import IntegrationNotConfiguredError
from tricycle_reaction_db import (
    CalculationLogBatchSpec,
    McpReactionDatabaseAdapter,
    ReactionBatchImportRequest,
    ReactionDatabaseMcpConfig,
    ReactionDatabaseProjectSpec,
    ReactionFilterRequest,
    RitsDatasetExportRequest,
    RxnResidDatasetExportRequest,
    validate_export_receipt,
)


class _McpState:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.status_calls = 0
        self.project_id = "project-001"
        self.import_job_id = "import-001"


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
                "register_import_manifest",
                "start_import_job",
                "get_import_status",
            }
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
        elif name == "register_import_manifest":
            manifest = arguments["manifest"]
            assert isinstance(manifest, dict)
            assert set(manifest) == {"archive_sha256", "entries"}
            entries = manifest["entries"]
            assert isinstance(entries, list) and len(entries) == 2
            assert all("file_sha256" in entry for entry in entries)
            assert all("staged_file_path" in entry for entry in entries)
            data = {"import_job_id": self.state.import_job_id}
        elif name == "start_import_job":
            data = {"status": "processing", "import_job_id": self.state.import_job_id}
        elif name == "get_import_status":
            self.state.status_calls += 1
            data = (
                {"status": "processing"}
                if self.state.status_calls == 1
                else {
                    "status": "succeeded",
                    "total_count": 2,
                    "succeeded_count": 2,
                    "failed_count": 0,
                    "inferred_reaction_count": 1,
                }
            )
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
        self.state.status_calls = 0

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
        staging_root = kwargs.pop("staging_root", "/srv/tricycle-stage")
        return McpReactionDatabaseAdapter(
            ReactionDatabaseMcpConfig(
                self.base_url,
                "test-token",
                organization_id="organization-001",
                staging_root=staging_root,
                poll_interval_seconds=0.001,
                wait_timeout_seconds=2,
                **kwargs,
            )
        )

    def test_import_uses_mcp_manifest_tools_without_sending_file_bytes(self) -> None:
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
                "register_import_manifest",
                "start_import_job",
                "get_import_status",
                "get_import_status",
            ],
        )
        self.assertFalse(any("multipart" in json.dumps(item).casefold() for item in self.state.requests))
        register = next(
            item
            for item in self.state.requests
            if item.get("method") == "tools/call"
            and item["params"]["name"] == "register_import_manifest"
        )
        entries = register["params"]["arguments"]["manifest"]["entries"]
        self.assertTrue(all(entry["staged_file_path"].startswith("/srv/tricycle-stage/") for entry in entries))
        self.assertTrue(all(entry["is_gaussian_log"] for entry in entries))

    def test_import_requires_staging_root_and_organization_before_network_mutation(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "downloads"
            logs.mkdir()
            (logs / "one.log").write_text("Gaussian\n", encoding="utf-8")
            request = self._request(root)
            manifest = request.resolve_batch()

            with self.assertRaises(IntegrationNotConfiguredError):
                self._adapter(staging_root=None).ingest_calculation_logs(request, manifest)
            self.assertEqual(self.state.requests, [])

            with self.assertRaises(IntegrationNotConfiguredError):
                McpReactionDatabaseAdapter(
                    ReactionDatabaseMcpConfig(self.base_url, "test-token", staging_root="/stage")
                ).ingest_calculation_logs(request, manifest)
            self.assertEqual(self.state.requests, [])

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
