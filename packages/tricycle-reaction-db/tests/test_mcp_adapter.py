from __future__ import annotations

import csv
import io
import json
import os
import threading
import unittest
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, ClassVar
from unittest.mock import patch

from tricycle_core import IntegrationNotConfiguredError, InvalidRequestError
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


class NativeThermodynamicsExportTests(unittest.TestCase):
    def test_presence_filters_are_forwarded_on_every_page_and_recorded(self) -> None:
        with TemporaryDirectory() as directory:
            adapter = McpReactionDatabaseAdapter(ReactionDatabaseMcpConfig(
                endpoint_url="https://example.invalid/mcp", token="unused"
            ))
            calls = []
            header = "mapped_reaction_id,mapped_reaction_smiles,activation_gibbs_free_energy_kcal_mol\n"

            def page(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
                calls.append(dict(arguments))
                offset = arguments["offset"]
                return {"project_id": "project-one", "media_type": "text/csv",
                        "csv": header + f"shared,C=C.C=CC=C>>C1CC=CCC1,{offset}\n",
                        "row_count": 1, "next_offset": offset + 1, "has_more": offset == 0}

            destination = Path(directory) / "native.csv"
            with patch.object(adapter._transport, "call_tool", side_effect=page):
                receipt = adapter.export_thermodynamics_csv(
                    ReactionFilterRequest("project-one"), destination,
                    has_activation_gibbs_free_energy=True, has_reaction_gibbs_free_energy=False,
                )
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(c["has_activation_gibbs_free_energy"] is True for c in calls))
            self.assertTrue(all(c["has_reaction_gibbs_free_energy"] is False for c in calls))
            self.assertTrue(receipt["arguments"]["has_activation_gibbs_free_energy"])
            self.assertEqual([r["activation_gibbs_free_energy_kcal_mol"] for r in csv.DictReader(io.StringIO(destination.read_text()))], ["0", "1"])

    def test_presence_filter_violation_fails_without_local_filtering(self) -> None:
        with TemporaryDirectory() as directory:
            adapter = McpReactionDatabaseAdapter(ReactionDatabaseMcpConfig(
                endpoint_url="https://example.invalid/mcp", token="unused"
            ))
            destination = Path(directory) / "native.csv"
            destination.write_text("previous verified export\n")
            response = {"project_id": "project-one", "media_type": "text/csv",
                        "csv": "mapped_reaction_id,mapped_reaction_smiles,activation_gibbs_free_energy_kcal_mol\nshared,C=C.C=CC=C>>C1CC=CCC1,\n",
                        "row_count": 1, "next_offset": 1, "has_more": False}
            with patch.object(adapter._transport, "call_tool", return_value=response), self.assertRaisesRegex(ReactionDatabaseMcpError, "presence filter"):
                adapter.export_thermodynamics_csv(
                    ReactionFilterRequest("project-one"), destination,
                    has_activation_gibbs_free_energy=True,
                )
            self.assertEqual(destination.read_text(), "previous verified export\n")

    def test_preserves_profile_rows_across_repeated_and_omitted_headers(self) -> None:
        with TemporaryDirectory() as directory:
            header = "mapped_reaction_id,mapped_reaction_smiles,activation_gibbs_free_energy_kcal_mol,level_of_theory,products_running_time_seconds\n"
            first = 'rxn-1,C=C.C=CC=C>>C1CC=CCC1,12.123456789,"method,one",\n'
            second = "rxn-1,C=C.C=CC=C>>C1CC=CCC1,18.25,method-two,0\n"
            third = "rxn-2,C=C.C=CC=C>>C1CC=CCC1,,method-three,\n"
            responses = [
                {"csv": header + first, "header_included": True},
                {"csv": header + second, "header_included": False},
                {"csv": third, "header_included": False},
            ]
            calls: list[tuple[str, Mapping[str, Any]]] = []

            class Transport:
                def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
                    calls.append((name, arguments))
                    offset = arguments["offset"]
                    return {
                        **responses[offset], "project_id": "project-one",
                        "media_type": "text/csv; charset=utf-8", "row_count": 1,
                        "next_offset": offset + 1, "has_more": offset < 2,
                    }

            adapter = McpReactionDatabaseAdapter(ReactionDatabaseMcpConfig(
                endpoint_url="https://example.invalid/mcp", token="unused"
            ))
            destination = Path(directory) / "native.csv"
            with patch.object(adapter._transport, "call_tool", side_effect=Transport().call_tool):
                receipt = adapter.export_thermodynamics_csv(
                    ReactionFilterRequest(project_id="project-one"), destination
                )
            self.assertEqual(destination.read_text(), header + first + second + third)
            rows = list(csv.DictReader(io.StringIO(destination.read_text())))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["activation_gibbs_free_energy_kcal_mol"], "12.123456789")
            self.assertEqual(rows[2]["activation_gibbs_free_energy_kcal_mol"], "")
            self.assertEqual(receipt["row_count"], 3)
            self.assertEqual([args["offset"] for _, args in calls], [0, 1, 2])
            predicate = json.loads(calls[0][1]["filter_expression"])
            self.assertEqual(predicate["field"], "rxn_smarts")

    def test_rejects_cross_project_or_stalled_pages_without_replacing_output(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "native.csv"
            destination.write_text("original output\n")
            for project, more in (("wrong-project", False), ("project-one", True)):
                class Transport:
                    def call_tool(
                        self, name: str, arguments: Mapping[str, Any],
                        returned_project: str = project, has_more: bool = more,
                    ) -> Mapping[str, Any]:
                        return {
                            "project_id": returned_project, "media_type": "text/csv",
                            "csv": "mapped_reaction_id,mapped_reaction_smiles\n",
                            "row_count": 0, "next_offset": 0, "has_more": has_more,
                        }

                adapter = McpReactionDatabaseAdapter(ReactionDatabaseMcpConfig(
                    endpoint_url="https://example.invalid/mcp", token="unused"
                ))
                with patch.object(adapter._transport, "call_tool", side_effect=Transport().call_tool), self.assertRaises(ReactionDatabaseMcpError):
                    adapter.export_thermodynamics_csv(
                        ReactionFilterRequest(project_id="project-one"), destination
                    )
                self.assertEqual(destination.read_text(), "original output\n")


class _McpState:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.project_id = "project-001"
        self.projects = [
            {"id": "project-001", "slug": "space-001", "name": "Space 001"},
            {"id": "project-002", "slug": "space-002", "name": "Space 002"},
            {"id": "project-003", "slug": "space-003", "name": "Space 003"},
            {"id": "project-004", "slug": "space-004", "name": "Space 004"},
        ]


class _McpHandler(BaseHTTPRequestHandler):
    state: _McpState

    def log_message(self, format: str, *args: object) -> None:
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
    _no_proxy: ClassVar[str | None]
    state: ClassVar[_McpState]
    server: ClassVar[ThreadingHTTPServer]
    thread: ClassVar[threading.Thread]
    base_url: ClassVar[str]
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

    def _adapter(self, **kwargs: Any) -> McpReactionDatabaseAdapter:
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

    def test_obsolete_upload_mode_is_rejected_before_credentials_are_used(self) -> None:
        with self.assertRaisesRegex(InvalidRequestError, "UPLOAD_MODE"):
            ReactionDatabaseMcpConfig.from_env(environ={"TRICYCLE_REACTION_DB_MCP_UPLOAD_MODE": "mcp"})

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

    def test_batch_upload_uses_multipart_queue_without_file_mcp_tool(self) -> None:
        self._check_batch_upload(reject_second=False)

    def test_batch_upload_records_rejected_queue_items(self) -> None:
        self._check_batch_upload(reject_second=True)

    def _check_batch_upload(self, *, reject_second: bool) -> None:
        rest_paths: list[str] = []
        create_payload: dict[str, object] = {}

        with TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "downloads" / "worker-000"
            logs.mkdir(parents=True)
            (logs / "a.log").write_text("Gaussian A\n", encoding="utf-8")
            (logs / "b.log").write_text("Gaussian B\n", encoding="utf-8")
            request = self._request(root)
            adapter = self._adapter()

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
                self.assertIn(b"Gaussian B\n", data or b"")
                descriptors = create_payload["files"]
                assert isinstance(descriptors, list)
                items = [
                    {
                        "id": f"item-{index}",
                        "client_file_id": descriptor["client_file_id"],
                        "status": "failed" if reject_second and index == 1 else "staged",
                    }
                    for index, descriptor in enumerate(descriptors)
                    if isinstance(descriptor, dict)
                ]
                return json.dumps(items).encode(), {}

            adapter._rest_json = fake_rest_json  # type: ignore[method-assign]
            adapter._rest_request = fake_rest_request  # type: ignore[method-assign]
            receipt = adapter.ingest_calculation_logs(request, request.resolve_batch())

        self.assertEqual(receipt.status, "failed" if reject_second else "submitted")
        self.assertEqual(receipt.accepted_log_count, 1 if reject_second else 2)
        self.assertEqual(receipt.failed_log_count, int(reject_second))
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
