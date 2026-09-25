from __future__ import annotations

import base64
import csv
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
    validate_export_receipt,
)
from tricycle_reaction_db.mcp_adapter import _McpTransport


class _McpState:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.project_id = "project-001"
        self.projects = [
            {"id": "project-001", "slug": "space-001", "name": "Space 001"},
            {"id": "project-002", "slug": "space-002", "name": "Space 002"},
            {"id": "project-003", "slug": "space-003", "name": "Space 003"},
            {"id": "project-004", "slug": "space-004", "name": "Space 004"},
        ]
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
                "export_mapped_reaction_transition_state_geometries",
                "list_projects",
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
        elif name == "list_projects":
            data = self.state.projects
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
                "batch": {"id": f"batch-{filename}", "status": "active"},
                "item": {"id": f"item-{filename}", "status": "staged"},
            }
        elif name == "export_mapped_reaction_transition_state_geometries":
            after_binding_id = arguments.get("after_binding_id")
            geometry_id = "geometry-ts" if after_binding_id is None else "geometry-ts-2"
            binding_id = "binding-ts" if after_binding_id is None else "binding-ts-2"
            data = {
                "project_id": arguments["project_id"],
                "records": [
                    {
                        "schema": "mapped-reaction-ts-geometry-v2",
                        "key": "[C:1][O:2]>>[C:1][O:2]",
                        "value": {
                            "geometry": {
                                "geometry_id": geometry_id,
                                "geometry_binding_id": binding_id,
                                "geometry_hash": "sha256:fake",
                                "charge": 0,
                                "multiplicity": 1,
                                "coordinate_units": "angstrom",
                                "atoms": [
                                    {
                                        "element": "O",
                                        "atomic_number": 8,
                                        "atom_map_number": 2,
                                        "coordinates_angstrom": [3.0, 4.0, 5.0],
                                    },
                                    {
                                        "element": "C",
                                        "atomic_number": 6,
                                        "atom_map_number": 1,
                                        "coordinates_angstrom": [0.0, 1.0, 2.0],
                                    }
                                ],
                            },
                            "rdkit_mol": {"format": "molblock", "value": "molblock"},
                        },
                    }
                ],
                "has_more": after_binding_id is None,
                "next_after_binding_id": "binding-ts" if after_binding_id is None else None,
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
                                    "mapped_reaction_smiles": "[C:1][O:2]>>[C:1][O:2]",
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
        kwargs.setdefault("upload_mode", "mcp")
        return McpReactionDatabaseAdapter(
            ReactionDatabaseMcpConfig(
                self.base_url,
                "test-token",
                organization_id="organization-001",
                **kwargs,
            )
        )

    def test_list_projects_uses_read_only_mcp_tool(self) -> None:
        projects = self._adapter().list_projects()

        self.assertEqual([project["id"] for project in projects], [
            "project-001",
            "project-002",
            "project-003",
            "project-004",
        ])
        tool_calls = [
            item["params"]["name"]
            for item in self.state.requests
            if item.get("method") == "tools/call"
        ]
        self.assertEqual(tool_calls, ["list_projects"])
        arguments = next(
            item["params"]["arguments"]
            for item in self.state.requests
            if item.get("method") == "tools/call"
        )
        assert isinstance(arguments, dict)
        self.assertEqual(arguments["include_archived"], False)
        self.assertEqual(arguments["organization_id"], "organization-001")

    def test_from_env_accepts_only_mcp_variable_names(self) -> None:
        with TemporaryDirectory() as temp:
            env_file = Path(temp) / ".env"
            env_file.write_text(
                "TRICYCLE_REACTION_DB_URL=http://legacy.example\n"
                "TRICYCLE_REACTION_DB_TOKEN=legacy-token\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(IntegrationNotConfiguredError, "MCP_URL"):
                ReactionDatabaseMcpConfig.from_env(env_file, environ={})

    def test_import_uploads_each_file_bytes_through_mcp(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "downloads" / "worker-000"
            logs.mkdir(parents=True)
            (logs / "a.log").write_text("Gaussian A\n", encoding="utf-8")
            (logs / "b.log").write_text("Gaussian B\n", encoding="utf-8")
            request = self._request(root)

            receipt = self._adapter().ingest_calculation_logs(request, request.resolve_batch())

        self.assertEqual(receipt.status, "submitted")
        self.assertEqual(receipt.project_id, self.state.project_id)
        self.assertEqual(receipt.accepted_log_count, 2)
        self.assertIsNone(receipt.cleaned_reaction_count)
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
        self.assertEqual(receipt.metadata["file_transfer"], "base64-per-file-queue")
        self.assertEqual(receipt.metadata["queue_mode"], "durable-upload-batch")
        self.assertEqual(receipt.metadata["queue_batch_count"], 2)
        self.assertEqual(len(receipt.metadata["queue_submissions"]), 2)
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

    def test_batch_upload_uses_multipart_queue_without_file_mcp_tool(self) -> None:
        self.state.include_upload_tool = False
        rest_paths: list[str] = []
        create_payload: dict[str, object] = {}

        with TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "downloads" / "worker-000"
            logs.mkdir(parents=True)
            (logs / "a.log").write_text("Gaussian A\n", encoding="utf-8")
            (logs / "b.log").write_text("Gaussian B\n", encoding="utf-8")
            request = self._request(root)
            adapter = self._adapter(upload_mode="batch")

            def fake_rest_json(
                method: str,
                path: str,
                payload: object | None = None,
            ) -> object:
                self.assertEqual(method, "POST")
                rest_paths.append(path)
                assert isinstance(payload, dict)
                create_payload.update(payload)
                return {"id": "batch-bulk-001", "status": "active"}

            def fake_rest_request(
                method: str,
                path: str,
                data: bytes | None,
                *,
                content_type: str | None,
            ) -> tuple[bytes, dict[str, str]]:
                self.assertEqual(method, "POST")
                rest_paths.append(path)
                self.assertIsNotNone(data)
                self.assertIsNotNone(content_type)
                self.assertIn(b"Gaussian A\n", data or b"")
                descriptors = create_payload["files"]
                assert isinstance(descriptors, list)
                items = [
                    {
                        "id": f"item-{index}",
                        "client_file_id": descriptor["client_file_id"],
                        "status": "staged",
                    }
                    for index, descriptor in enumerate(descriptors)
                    if isinstance(descriptor, dict)
                ]
                return json.dumps(items).encode(), {}

            adapter._rest_json = fake_rest_json  # type: ignore[method-assign]
            adapter._rest_request = fake_rest_request  # type: ignore[method-assign]
            receipt = adapter.ingest_calculation_logs(request, request.resolve_batch())

        self.assertEqual(receipt.status, "submitted")
        self.assertEqual(receipt.accepted_log_count, 2)
        self.assertEqual(receipt.metadata["file_transfer"], "multipart-batch-queue")
        self.assertEqual(receipt.metadata["queue_batch_count"], 1)
        self.assertEqual(rest_paths, ["/api/upload-batches", "/api/upload-batches/batch-bulk-001/files"])
        self.assertNotIn("archive_sha256", create_payload)
        self.assertNotIn("manifest_sha256", create_payload)
        self.assertNotIn("manifest_schema_version", create_payload)
        descriptors = create_payload["files"]
        assert isinstance(descriptors, list)
        self.assertEqual(
            [descriptor["relative_path"] for descriptor in descriptors if isinstance(descriptor, dict)],
            ["downloads/worker-000/a.log", "downloads/worker-000/b.log"],
        )
        tool_calls = [
            item["params"]["name"]
            for item in self.state.requests
            if item.get("method") == "tools/call"
        ]
        self.assertEqual(tool_calls, ["create_project"])
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
            "database.upload-batches.multipart",
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

    def test_rate_limit_retry_after_uses_header_or_json_detail(self) -> None:
        self.assertEqual(_McpTransport._retry_after_seconds({"Retry-After": "7"}, ""), 7.0)
        self.assertEqual(
            _McpTransport._retry_after_seconds(
                {}, '{"detail":{"retry_after_seconds": 3}}'
            ),
            3.0,
        )

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

    def test_rits_export_uses_paged_mcp_geometry_tool_and_database_filter(self) -> None:
        adapter = self._adapter()
        filtered = adapter.filter_cleaned_reactions(ReactionFilterRequest("project-001"))
        self.assertEqual(filtered.selected_count, 1)
        self.assertEqual(filtered.metadata["transport"], "mcp")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            rits = adapter.export_rits_dataset(
                RitsDatasetExportRequest("project-001", root / "rits.csv")
            )
            self.assertEqual(rits.row_count, 2)
            with (root / "rits.csv").open(encoding="utf-8", newline="") as handle:
                rits_rows = list(csv.reader(handle))
            self.assertEqual(rits_rows[1][0], "[C:1][O:2]>>[C:1][O:2]")
            self.assertIn("geometry_id=geometry-ts", rits_rows[1][1])
            self.assertIn("geometry_binding_id=binding-ts", rits_rows[1][1])
            self.assertEqual(rits_rows[1][1].splitlines()[2:], ["C 0 1 2", "O 3 4 5"])
        validate_export_receipt(
            RitsDatasetExportRequest("project-001", Path("rits.csv")), rits
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
        geometry_calls = [
            item["params"]
            for item in self.state.requests
            if item.get("method") == "tools/call"
            and item["params"]["name"] == "export_mapped_reaction_transition_state_geometries"
        ]
        self.assertEqual(len(geometry_calls), 2)
        self.assertEqual(geometry_calls[0]["arguments"]["limit"], 64)
        self.assertIsNone(geometry_calls[0]["arguments"]["after_binding_id"])
        self.assertEqual(geometry_calls[1]["arguments"]["after_binding_id"], "binding-ts")

    def test_rits_export_still_accepts_v1_geometry_records(self) -> None:
        mapped_smiles, xyz = McpReactionDatabaseAdapter._mapped_geometry_record_xyz(
            {
                "schema": "mapped-reaction-ts-geometry-v1",
                "key": "[C:1]>>[C:1]",
                "value": {
                    "geometry": {
                        "geometry_id": "geometry-v1",
                        "coordinate_units": "angstrom",
                        "atoms": [
                            {
                                "element": "C",
                                "atom_map_number": 1,
                                "coordinates_angstrom": [0.0, 1.0, 2.0],
                            }
                        ],
                    }
                },
            }
        )

        self.assertEqual(mapped_smiles, "[C:1]>>[C:1]")
        self.assertEqual(xyz, "1\ngeometry_id=geometry-v1\nC 0 1 2\n")


if __name__ == "__main__":
    unittest.main()
