from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tricycle_core import ExecutionContext, InvalidRequestError, TaskStatus
from tricycle_reaction_db import (
    CYCLOADDITION_RXN_SMARTS,
    RITS_EXPORT_API_CONTRACT,
    RXNRESID_EXPORT_API_CONTRACT,
    CalculationLogBatchSpec,
    ReactionBatchImportRequest,
    ReactionDatabaseImportReceipt,
    ReactionDatabaseMcpToolCall,
    ReactionDatabasePlugin,
    ReactionDatabaseProjectSpec,
    ReactionFilterReceipt,
    ReactionFilterRequest,
    ReactionSpaceExportReceipt,
    RitsDatasetExportRequest,
    RxnResidDatasetExportRequest,
)


class _FakeDatabaseAdapter:
    def __init__(self) -> None:
        self.import_calls = []
        self.filter_calls = []
        self.rits_calls = []
        self.rxnresid_calls = []

    def ingest_calculation_logs(self, request, manifest):
        self.import_calls.append((request, manifest))
        return ReactionDatabaseImportReceipt(
            project_id="project-uuid-1",
            accepted_log_count=len(manifest.files),
            cleaned_reaction_count=1,
        )

    def filter_cleaned_reactions(self, request):
        self.filter_calls.append(request)
        return ReactionFilterReceipt(
            project_id=request.project_id,
            selected_count=1,
            reaction_smarts=request.reaction_smarts,
        )

    def export_rits_dataset(self, request):
        self.rits_calls.append(request)
        return ReactionSpaceExportReceipt(
            request.destination,
            row_count=1,
            api_contract=request.api_contract,
            mcp_tool_calls=_mcp_export_calls(
                request.project_id,
                request.reaction_smarts,
                "rits",
            ),
        )

    def export_rxnresid_dataset(self, request):
        self.rxnresid_calls.append(request)
        return ReactionSpaceExportReceipt(
            request.destination,
            row_count=1,
            api_contract=request.api_contract,
            mcp_tool_calls=_mcp_export_calls(
                request.project_id,
                request.reaction_smarts,
                "rxnresid",
            ),
        )


def _mcp_export_calls(
    project_id: str,
    reaction_smarts: str,
    export_kind: str,
) -> tuple[ReactionDatabaseMcpToolCall, ...]:
    operations = (
        (
            "MappedReactionQueryService.list_mapped_reactions",
            "reaction-list",
        ),
        (
            "MappedReactionQueryService.get_mapped_reaction",
            "reaction-detail",
        ),
        ("GeometryQueryService.get_geometry", "geometry"),
    )
    if export_kind == "rxnresid":
        operations = (
            operations[0],
            ("ReactionEnergyQueryService.get_mapped_reaction_thermodynamics", "energy"),
        )
    return tuple(
        ReactionDatabaseMcpToolCall(
            "compose_query",
            {
                "app_name": "test-reaction-db",
                "operation": operation,
                "project_id": project_id,
                "reaction_smarts": reaction_smarts,
                "resource": resource,
            },
        )
        for operation, resource in operations
    )


class _FailingDatabaseAdapter:
    def __getattr__(self, name):
        raise AssertionError(f"dry-run called adapter method: {name}")


class ReactionDatabaseBatchWorkflowTests(unittest.TestCase):
    def _request(self, root: Path) -> ReactionBatchImportRequest:
        logs = root / "downloads" / "worker-000"
        logs.mkdir(parents=True)
        (logs / "ts-001.log").write_text("Gaussian log\n", encoding="utf-8")
        (logs / "ts-002.log").write_text("Gaussian log\n", encoding="utf-8")
        (root / "worker.log").write_text("Bohr worker log\n", encoding="utf-8")
        (root / "input.gjf").write_text("Gaussian input\n", encoding="utf-8")
        (root / "metadata.json").write_text("{}\n", encoding="utf-8")
        return ReactionBatchImportRequest(
            project=ReactionDatabaseProjectSpec(
                slug="dpa-finetuned-rits-ts-001",
                name="DPA-finetuned RitS TS 001",
            ),
            batch=CalculationLogBatchSpec(
                batch_id="bohr-batch-001",
                root=root,
                metadata={"bohr_job_group_id": "7335192", "bohr_project_id": "991668"},
            ),
            provenance={
                "workflow": "DPA-finetuned RitS initial guess -> Gaussian TS optimization",
                "initial_guess_source": "DPA-finetuned RitS",
                "engine": "Gaussian",
            },
        )

    def test_import_scope_selects_only_logs_and_one_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            request = self._request(Path(temp))
            manifest = request.resolve_batch()
            self.assertEqual(len(manifest.files), 2)
            self.assertTrue(all(path.suffix == ".log" for path in manifest.files))
            self.assertEqual(manifest.total_bytes, sum(path.stat().st_size for path in manifest.files))

            plan = ReactionDatabasePlugin().plan_batch_import(
                request, ExecutionContext(run_id="plan-1", workdir=Path(temp))
            )
            self.assertEqual(plan.metadata["batch_id"], "bohr-batch-001")
            self.assertEqual(plan.metadata["log_count"], 2)
            self.assertEqual(len(plan.inputs), 1)
            self.assertEqual(plan.parameters["project"]["slug"], "dpa-finetuned-rits-ts-001")
            self.assertNotIn("input.gjf", str(plan.to_dict()))
            self.assertNotIn("metadata.json", str(plan.to_dict()))

    def test_from_mapping_rejects_more_than_one_batch_in_one_project_request(self) -> None:
        payload = {
            "project": {"slug": "one-project", "name": "One project"},
            "batches": [
                {"batch_id": "one", "root": "/tmp/one"},
                {"batch_id": "two", "root": "/tmp/two"},
            ],
            "provenance": {"workflow": "w", "initial_guess_source": "s"},
        }
        with self.assertRaisesRegex(InvalidRequestError, "exactly one batch"):
            ReactionBatchImportRequest.from_mapping(payload)

    def test_batch_import_rejects_existing_internal_project_id(self) -> None:
        payload = {
            "project_id": "existing-project-uuid",
            "project": {"slug": "one-project", "name": "One project"},
            "batch": {"batch_id": "one", "root": "/tmp/one"},
            "provenance": {"workflow": "w", "initial_guess_source": "s"},
        }
        with self.assertRaisesRegex(InvalidRequestError, "project_id is not accepted"):
            ReactionBatchImportRequest.from_mapping(payload)

    def test_bohr_import_requires_job_group_and_project_provenance(self) -> None:
        with self.assertRaisesRegex(InvalidRequestError, "bohr_job_group_id"):
            ReactionBatchImportRequest(
                project=ReactionDatabaseProjectSpec("one-project", "One project"),
                batch=CalculationLogBatchSpec("one", Path("/tmp/one")),
                provenance={"workflow": "w", "initial_guess_source": "s"},
            )

    def test_import_rejects_symlinked_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root.parent / f"{root.name}-outside.log"
            outside.write_text("outside\n", encoding="utf-8")
            try:
                logs = root / "downloads"
                logs.mkdir()
                (logs / "outside.log").symlink_to(outside)
                request = self._request_without_files(root)
                with self.assertRaisesRegex(InvalidRequestError, "symlinks"):
                    request.resolve_batch()
            finally:
                outside.unlink(missing_ok=True)

    def test_dry_run_never_calls_database_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            request = self._request(Path(temp))
            result = ReactionDatabasePlugin().ingest_batch(
                request,
                _FailingDatabaseAdapter(),
                ExecutionContext(run_id="dry-1", workdir=Path(temp), dry_run=True),
            )
            self.assertEqual(result.status, TaskStatus.PLANNED)

    def test_live_adapter_receives_project_scoped_import_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = self._request(root)
            adapter = _FakeDatabaseAdapter()
            plugin = ReactionDatabasePlugin()
            live = ExecutionContext(run_id="live-1", workdir=root, dry_run=False)

            imported = plugin.ingest_batch(request, adapter, live)
            self.assertEqual(imported.status, TaskStatus.SUCCEEDED)
            self.assertEqual(imported.external_id, "project-uuid-1")
            self.assertEqual(len(adapter.import_calls), 1)

            filtered = plugin.filter_reactions(
                ReactionFilterRequest("project-uuid-1"), adapter, live
            )
            self.assertEqual(filtered.metadata["selected_count"], 1)
            self.assertEqual(adapter.filter_calls[0].reaction_smarts, CYCLOADDITION_RXN_SMARTS)
            self.assertEqual(filtered.metadata["reaction_smarts"], CYCLOADDITION_RXN_SMARTS)

            rits = plugin.export_rits(
                RitsDatasetExportRequest("project-uuid-1", root / "rits.csv"), adapter, live
            )
            self.assertEqual(rits.outputs[0].media_type, "text/csv")
            self.assertEqual(adapter.rits_calls[0].structure_format, "xyz")
            self.assertEqual(adapter.rits_calls[0].reaction_smarts, CYCLOADDITION_RXN_SMARTS)

            rxnresid = plugin.export_rxnresid(
                RxnResidDatasetExportRequest("project-uuid-1", root / "rxnresid.csv"),
                adapter,
                live,
            )
            self.assertEqual(rxnresid.outputs[0].metadata["required_columns"][-1], "product_compute_seconds")
            self.assertEqual(adapter.rxnresid_calls[0].reaction_smarts, CYCLOADDITION_RXN_SMARTS)

    def test_export_requests_use_internal_project_id_and_database_filter(self) -> None:
        with self.assertRaisesRegex(InvalidRequestError, "project_id"):
            ReactionFilterRequest.from_mapping(
                {"project_slug": "not-an-internal-project-id"}
            )
        rits = RitsDatasetExportRequest("00000000-project", Path("rits.csv"))
        rxnresid = RxnResidDatasetExportRequest("00000000-project", Path("rxnresid.csv"))
        self.assertEqual(ReactionFilterRequest("00000000-project").reaction_smarts, CYCLOADDITION_RXN_SMARTS)
        self.assertEqual(rits.to_dict()["required_columns"], ["mapped_reaction_smiles", "ts_structure"])
        self.assertEqual(rits.to_dict()["reaction_smarts"], CYCLOADDITION_RXN_SMARTS)
        self.assertIn("activation_free_energy", rxnresid.to_dict()["required_columns"])

    def test_database_filter_rejects_a_different_reaction_smarts(self) -> None:
        with self.assertRaisesRegex(InvalidRequestError, "reaction_smarts"):
            ReactionFilterRequest(
                "00000000-project",
                reaction_smarts="[C]=[C]>>[C]1[C][C]1",
            )

    def test_database_filter_rejects_explicit_reaction_class(self) -> None:
        with self.assertRaisesRegex(InvalidRequestError, "reaction_class"):
            ReactionFilterRequest.from_mapping(
                {
                    "project_id": "00000000-project",
                    "reaction_class": "cycloaddition",
                }
            )

    def test_successful_export_rejects_receipt_without_mcp_provenance(self) -> None:
        class _IncompleteRxnResidAdapter(_FakeDatabaseAdapter):
            def export_rxnresid_dataset(self, request):
                return ReactionSpaceExportReceipt(
                    request.destination,
                    row_count=1,
                    api_contract=RXNRESID_EXPORT_API_CONTRACT,
                    mcp_tool_calls=(
                        ReactionDatabaseMcpToolCall(
                            "compose_query",
                            {
                                "app_name": "test-reaction-db",
                                "operation": "MappedReactionQueryService.list_mapped_reactions",
                                "project_id": request.project_id,
                                "reaction_smarts": request.reaction_smarts,
                            },
                        ),
                    ),
                )

        request = RxnResidDatasetExportRequest("00000000-project", Path("rxnresid.csv"))
        with self.assertRaisesRegex(InvalidRequestError, "thermodynamics"):
            ReactionDatabasePlugin().export_rxnresid(
                request,
                _IncompleteRxnResidAdapter(),
                ExecutionContext(run_id="incomplete-mcp", workdir=Path("."), dry_run=False),
            )

    def test_successful_rits_export_requires_all_mcp_queries(self) -> None:
        class _IncompleteRitsAdapter(_FakeDatabaseAdapter):
            def export_rits_dataset(self, request):
                return ReactionSpaceExportReceipt(
                    request.destination,
                    row_count=1,
                    api_contract=RITS_EXPORT_API_CONTRACT,
                    mcp_tool_calls=(
                        ReactionDatabaseMcpToolCall(
                            "compose_query",
                            {
                                "app_name": "test-reaction-db",
                                "operation": "MappedReactionQueryService.list_mapped_reactions",
                                "project_id": request.project_id,
                                "reaction_smarts": request.reaction_smarts,
                            },
                        ),
                    ),
                )

        request = RitsDatasetExportRequest("00000000-project", Path("rits.csv"))
        with self.assertRaisesRegex(InvalidRequestError, "RitS MCP export"):
            ReactionDatabasePlugin().export_rits(
                request,
                _IncompleteRitsAdapter(),
                ExecutionContext(run_id="incomplete-mcp", workdir=Path("."), dry_run=False),
            )

    @staticmethod
    def _request_without_files(root: Path) -> ReactionBatchImportRequest:
        return ReactionBatchImportRequest(
            project=ReactionDatabaseProjectSpec("one-project", "One project"),
            batch=CalculationLogBatchSpec(
                "one",
                root,
                metadata={"bohr_job_group_id": "1", "bohr_project_id": "2"},
            ),
            provenance={"workflow": "w", "initial_guess_source": "s"},
        )


if __name__ == "__main__":
    unittest.main()
