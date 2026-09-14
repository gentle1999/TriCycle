from __future__ import annotations

import unittest
from pathlib import Path

from tricycle_core import (
    Artifact,
    ExecutionContext,
    InvalidRequestError,
    PluginKind,
    PluginManifest,
    TaskPlan,
    TaskResult,
    TaskStatus,
    canonical_json,
    sha256_value,
)


class CoreContractTests(unittest.TestCase):
    def test_public_models_serialize_nested_artifacts(self) -> None:
        input_artifact = Artifact(
            artifact_id="input",
            location="inputs/request.toml",
            media_type="application/toml",
        )
        plan = TaskPlan(
            task_id="task-001",
            plugin_id="tricycle.core.test",
            operation="validate",
            inputs=(input_artifact,),
            parameters={"attempt": 1},
        )
        result = TaskResult(
            task_id=plan.task_id,
            plugin_id=plan.plugin_id,
            status=TaskStatus.PLANNED,
            outputs=(Artifact("plan", "outputs/plan.toml"),),
        )

        self.assertEqual(plan.to_dict()["inputs"], [input_artifact.to_dict()])
        self.assertEqual(result.to_dict()["status"], "planned")
        self.assertEqual(result.to_dict()["outputs"][0]["artifact_id"], "plan")

    def test_manifest_and_execution_context_keep_stable_contracts(self) -> None:
        manifest = PluginManifest(
            plugin_id="tricycle.core.test",
            display_name="Core test plugin",
            version="1.0.0",
            kind=PluginKind.SAMPLING,
            description="test manifest",
            capabilities=("test.validate",),
        )
        context = ExecutionContext(
            run_id="run-001",
            workdir=Path("runs"),
            metadata={"source": "unit-test"},
        )

        self.assertEqual(manifest.to_dict()["kind"], "sampling")
        self.assertEqual(context.to_dict()["workdir"], "runs")
        self.assertEqual(context.to_dict()["metadata"], {"source": "unit-test"})

    def test_canonical_hash_does_not_depend_on_mapping_order(self) -> None:
        first = {"z": [1, 2], "a": {"nested": True}}
        second = {"a": {"nested": True}, "z": [1, 2]}

        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(sha256_value(first), sha256_value(second))

    def test_core_models_reject_missing_required_identifiers(self) -> None:
        with self.assertRaisesRegex(InvalidRequestError, "artifact_id"):
            Artifact("", "location")
        with self.assertRaisesRegex(InvalidRequestError, "run_id"):
            ExecutionContext("")
        with self.assertRaisesRegex(InvalidRequestError, "plugin_id"):
            PluginManifest("", "name", "1.0.0", PluginKind.SAMPLING, "description")


if __name__ == "__main__":
    unittest.main()
