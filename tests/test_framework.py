import unittest

from tricycle_agent.bootstrap import create_registry
from tricycle_bohr import DryRunBohrGateway
from tricycle_core import Artifact, ExecutionContext, ReactionRecord, TaskStatus
from tricycle_reaction_db import (
    InMemoryReactionRepository,
    ReactionDatabasePlugin,
    ReactionIngestRequest,
)
from tricycle_reactionopt import ReactionConfigRequest, ReactionOptPlugin
from tricycle_rits import RitsPlugin
from tricycle_sampling import Candidate, SamplingPlugin, SamplingRequest
from tricycle_sar import SarPlugin


class FrameworkSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ExecutionContext(run_id="test-run")

    def test_registry_contains_registered_plugins(self) -> None:
        registry = create_registry()
        self.assertEqual(
            {manifest.plugin_id for manifest in registry.manifests()},
            {
                "tricycle.bohr.compute",
                "tricycle.bohr.submit",
                "tricycle.reaction-database",
                "tricycle.sar",
                "tricycle.rits",
                "tricycle.sampling",
                "tricycle.reactionopt",
                "tricycle.exploration",
            },
        )

    def test_sar_and_rits_target_bohr_for_train_and_infer(self) -> None:
        dataset = {"artifact_id": "dataset", "location": "s3://bucket/data"}
        checkpoint = {"artifact_id": "checkpoint", "location": "s3://bucket/model"}
        code = {"artifact_id": "source", "location": "/workspace/job", "kind": "directory"}

        sar = SarPlugin()
        sar_train = sar.plan(
            "train",
            {
                "model_name": "sar-v1",
                "image": "sar/image",
                "dataset": dataset,
                "code_artifact": code,
                "input_directory": "/workspace/job",
                "command": ["bash", "run.sh"],
            },
            self.context,
        )
        sar_infer = sar.plan(
            "infer",
            {
                "model_name": "sar-v1",
                "image": "sar/image",
                "checkpoint": checkpoint,
                "code_artifact": code,
                "input_directory": "/workspace/job",
                "command": ["bash", "run.sh"],
            },
            self.context,
        )
        self.assertTrue(sar_train.metadata["bohr_target"])
        self.assertEqual(sar_infer.parameters["bohr_job"]["operation"], "sar.infer")

        rits = RitsPlugin()
        rits_train = rits.plan(
            "train",
            {
                "model_name": "rits-v1",
                "image": "rits/image",
                "dataset": dataset,
                "code_artifact": code,
                "input_directory": "/workspace/job",
                "command": ["bash", "run.sh"],
            },
            self.context,
        )
        self.assertEqual(rits_train.parameters["bohr_job"]["operation"], "rits.train")

        submit_context = ExecutionContext(run_id="submit-run", dry_run=False)
        result = sar.submit_to_bohr(sar_train, DryRunBohrGateway(), submit_context)
        self.assertEqual(result.status, TaskStatus.SUBMITTED)

    def test_reaction_record_can_be_upserted(self) -> None:
        plugin = ReactionDatabasePlugin()
        repository = InMemoryReactionRepository()
        request = ReactionIngestRequest(
            record=ReactionRecord(
                reaction_id="rxn-1",
                reaction_smiles="[C:1]=[C:2]>>[C:1][C:2]",
                artifacts=(Artifact("log", "runs/rxn-1.log"),),
            )
        )
        result = plugin.ingest(
            request, repository, ExecutionContext(run_id="db-run", dry_run=False)
        )
        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertIsNotNone(repository.get("rxn-1"))

        validation = plugin.ingest(
            request.__class__(record=request.record, validate_only=True),
            repository,
            ExecutionContext(run_id="validation-run", dry_run=False),
        )
        self.assertEqual(validation.status, TaskStatus.PLANNED)

    def test_sampling_is_reproducible(self) -> None:
        plugin = SamplingPlugin()
        candidates = tuple(Candidate(str(index)) for index in range(8))
        request = SamplingRequest(candidates=candidates, budget=3, seed=42)
        first = plugin.select(request)
        second = plugin.select(request)
        self.assertEqual(first.selected_ids, second.selected_ids)
        self.assertEqual(len(first.remaining), 5)

    def test_reactionopt_generates_versioned_config(self) -> None:
        plugin = ReactionOptPlugin()
        request = ReactionConfigRequest(
            reaction_id="rxn-1",
            reaction_smiles="[C:1]=[C:2].[C:3]=[C:4]>>[C:1]1[C:2][C:3][C:4]1",
        )
        config = plugin.generate(request)
        self.assertEqual(config.schema_version, "tricycle.reactionopt/v1")
        plan = plugin.plan_generate(request, self.context)
        self.assertEqual(plan.parameters["config"]["reaction_id"], "rxn-1")


if __name__ == "__main__":
    unittest.main()
