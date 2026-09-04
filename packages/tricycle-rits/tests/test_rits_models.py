import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tricycle_bohrsubmit import InMemoryBohrGateway
from tricycle_core import Artifact, ExecutionContext, ModelMode, TaskStatus
from tricycle_core.errors import InvalidRequestError
from tricycle_rits import (
    RitsJobRequest,
    RitsPlugin,
    build_finetune_command,
    build_inference_command,
    build_resume_command,
    build_training_command,
    load_default_model_asset,
)


class RitsModelTests(unittest.TestCase):
    def test_default_model_asset_pins_source_and_checkpoint_location(self) -> None:
        asset = load_default_model_asset()

        self.assertEqual(asset.model_name, "rits-rgd1-chembl-extended")
        self.assertEqual(asset.repository, "https://github.com/gentle1999/RitS")
        self.assertEqual(
            asset.revision,
            "6d8aa0704c21d2798326a7e070491abf438ca292",
        )
        self.assertEqual(asset.checkpoint_name, "rits.ckpt")
        self.assertEqual(asset.resolve_checkpoint(), Path("artifacts/rits/rits.ckpt"))
        self.assertEqual(asset.task_checkpoint_path, "data/rits.ckpt")
        self.assertEqual(
            asset.checkpoint_source.split("id=")[-1], "1tmCmIuzgDsZYg3DeMeA0fcKDqvbCyqye"
        )
        self.assertEqual(
            asset.checkpoint_sha256,
            "2c6fdacc13a304cb8bb4030881ff4198de647d8f8f7fdaa878414e2514be0668",
        )

    def test_missing_default_checkpoint_has_actionable_message(self) -> None:
        asset = load_default_model_asset()

        with self.assertRaisesRegex(InvalidRequestError, "artifacts/rits/rits.ckpt"):
            asset.require_checkpoint("/tmp/tricycle-rits-no-checkpoint")

    def test_ml_worker_defaults_can_come_from_environment(self) -> None:
        request = {
            "model_name": "rits-v1",
            "checkpoint": {"artifact_id": "checkpoint", "location": "models/model.pt"},
            "code_artifact": {
                "artifact_id": "rits-source",
                "location": "RitS",
                "kind": "directory",
            },
            "input_directory": "RitS",
            "command": [
                "bash",
                "run.sh",
            ],
        }
        with patch.dict(
            os.environ,
            {
                "BOHR_ML_IMAGE": "registry.example/dl:rits",
                "BOHR_PROJECT_ID": "project-002",
                "BOHR_MACHINE_TYPE": "gpu-machine",
            },
            clear=False,
        ):
            plan = RitsPlugin().plan("infer", request, ExecutionContext(run_id="rits-env"))

        job = plan.parameters["bohr_job"]
        self.assertEqual(job["image"], "registry.example/dl:rits")
        self.assertEqual(job["project_id"], "project-002")
        self.assertEqual(job["machine_type"], "gpu-machine")

    def test_shared_model_contract_preserves_code_and_input_directory(self) -> None:
        request = RitsJobRequest(
            model_name="rits-v1",
            mode=ModelMode.TRAIN,
            image="registry.example/dl:rits",
            code_artifact=Artifact("rits-source", "RitS", kind="directory"),
            dataset=Artifact("dataset", "data/rits_dataset", kind="directory"),
            input_directory="./job-bundle",
            command=("bash", "run.sh"),
        )

        job = request.to_bohr_job()

        self.assertEqual(job.operation, "rits.train")
        self.assertEqual(job.parameters["input_directory"], "./job-bundle")
        self.assertEqual(
            [artifact.artifact_id for artifact in job.input_artifacts],
            ["rits-source", "dataset"],
        )

    def test_submission_uses_common_bohr_job_executor(self) -> None:
        plan = RitsPlugin().plan(
            "infer",
            {
                "model_name": "rits-v1",
                "image": "registry.example/dl:rits",
                "code_artifact": {
                    "artifact_id": "rits-source",
                    "location": "RitS",
                    "kind": "directory",
                },
                "checkpoint": {"artifact_id": "checkpoint", "location": "models/model.pt"},
                "input_directory": "RitS",
                "command": ["bash", "run.sh"],
            },
            ExecutionContext(run_id="rits-submit"),
        )
        gateway = InMemoryBohrGateway()

        result = RitsPlugin().submit_to_bohr(
            plan,
            gateway,
            ExecutionContext(run_id="rits-submit", dry_run=False),
        )

        self.assertEqual(result.status, TaskStatus.SUBMITTED)
        self.assertEqual(result.plugin_id, "tricycle.rits")
        self.assertTrue(result.metadata["bohr_target"])
        submitted = next(iter(gateway.jobs.values()))
        self.assertEqual(submitted.operation, "rits.infer")
        self.assertEqual(submitted.input_artifacts[0].artifact_id, "rits-source")

    def test_official_rits_command_builders(self) -> None:
        train = build_training_command(overrides=("train.gpus=1", "data.batch_size=2"))
        self.assertEqual(train[0], "python3.12")
        self.assertIn("scripts/train.py", train)
        self.assertIn("train.gpus=1", train)

        infer = build_inference_command(reactant_smi="[C:1][H:2]", product_smi="[C:1]=[H:2]")
        self.assertIn("scripts/sample_transition_state.py", infer)
        self.assertIn("--reactant_smi", infer)
        self.assertIn("--product_smi", infer)

        finetune = build_finetune_command(
            checkpoint="data/rits.ckpt",
            overrides=("data.dataset_root=./data/rits_dataset",),
        )
        self.assertIn("finetune_from=data/rits.ckpt", finetune)
        self.assertNotIn("resume=data/rits.ckpt", finetune)

        resume = build_resume_command(checkpoint="outputs/run/checkpoints/last.ckpt")
        self.assertIn("resume=outputs/run/checkpoints/last.ckpt", resume)

        reaction_file = build_inference_command(reaction_file="data/reactions.smi")
        self.assertIn("--reaction_file", reaction_file)
        self.assertIn("--num_steps", reaction_file)

        with self.assertRaises(InvalidRequestError):
            build_finetune_command(checkpoint="", overrides=())

        with self.assertRaises(InvalidRequestError):
            build_training_command(finetune_from="data/a.ckpt", resume="outputs/last.ckpt")

        with self.assertRaises(InvalidRequestError):
            build_inference_command(reactant_smi="C")

    def test_finetune_plan_requires_the_rits_dataset_and_checkpoint(self) -> None:
        request = {
            "model_name": "rits-finetune",
            "image": "registry.example/dl:rits",
            "dataset": {"artifact_id": "dataset", "location": "data/rits_dataset"},
            "checkpoint": {"artifact_id": "checkpoint", "location": "data/rits.ckpt"},
            "code_artifact": {
                "artifact_id": "rits-source",
                "location": "RitS",
                "kind": "directory",
            },
            "input_directory": "RitS",
            "command": ["bash", "run.sh"],
        }
        plan = RitsPlugin().plan("finetune", request, ExecutionContext(run_id="rits-finetune"))
        self.assertEqual(plan.parameters["bohr_job"]["operation"], "rits.finetune")
        self.assertEqual(plan.metadata["mode"], "finetune")
        self.assertEqual(
            plan.parameters["bohr_job"]["parameters"]["upstream_protocol"], "finetune_from"
        )

        request.pop("dataset")
        with self.assertRaisesRegex(InvalidRequestError, "dataset"):
            RitsPlugin().plan("finetune", request, ExecutionContext(run_id="rits-finetune-no-data"))

    def test_task_command_is_required(self) -> None:
        with self.assertRaises(InvalidRequestError):
            RitsPlugin().plan(
                "infer",
                {
                    "model_name": "rits-v1",
                    "image": "registry.example/runtime:latest",
                    "code_artifact": {
                        "artifact_id": "rits-source",
                        "location": "RitS",
                        "kind": "directory",
                    },
                    "checkpoint": {"artifact_id": "checkpoint", "location": "models/model.pt"},
                    "input_directory": "RitS",
                },
                ExecutionContext(run_id="rits-command-required"),
            )


if __name__ == "__main__":
    unittest.main()
