import os
import unittest
from unittest.mock import patch

from tricycle_core import Artifact, ExecutionContext, ModelMode
from tricycle_core.errors import InvalidRequestError
from tricycle_sar import (
    RxnResidPlugin,
    SarJobRequest,
    SarPlugin,
    build_finetune_command,
    build_inference_command,
    build_refit_command,
    build_training_command,
    load_default_model_asset,
)


class SarModelTests(unittest.TestCase):
    def test_default_model_asset_pins_rxnresid_repository(self) -> None:
        asset = load_default_model_asset()

        self.assertEqual(asset.model_name, "rxnresid")
        self.assertEqual(asset.repository, "https://github.com/gentle1999/RxnResid")
        self.assertEqual(
            asset.revision,
            "9deebde39b38df1085dbac3a379c2a59edd7c97d",
        )
        self.assertEqual(asset.config_path, "configs/rxnresid.yaml")
        self.assertEqual(asset.training_entrypoint, "train.py")
        self.assertEqual(asset.inference_entrypoint, "predict.py")
        self.assertEqual(asset.uncertainty_method, "evidential")
        self.assertEqual(
            asset.uncertainty_fields,
            (
                "aleatoric_variance",
                "epistemic_variance",
                "predictive_variance",
                "aleatoric_std",
                "epistemic_std",
                "predictive_std",
                "lower_95",
                "upper_95",
            ),
        )

    def test_ml_worker_defaults_can_come_from_environment(self) -> None:
        request = {
            "model_name": "rxnresid-v1",
            "dataset": {"artifact_id": "dataset", "location": "data/train.csv"},
            "code_artifact": {
                "artifact_id": "rxnresid-source",
                "location": "RxnResid",
                "kind": "directory",
            },
            "input_directory": "RxnResid",
            "command": [
                "bash",
                "run.sh",
            ],
        }
        with patch.dict(
            os.environ,
            {
                "BOHR_ML_IMAGE": "registry.example/dl:rxnresid",
                "BOHR_PROJECT_ID": "project-001",
                "BOHR_MACHINE_TYPE": "gpu-machine",
            },
            clear=False,
        ):
            plan = SarPlugin().plan("train", request, ExecutionContext(run_id="sar-env"))

        job = plan.parameters["bohr_job"]
        self.assertEqual(job["image"], "registry.example/dl:rxnresid")
        self.assertEqual(job["project_id"], "project-001")
        self.assertEqual(job["machine_type"], "gpu-machine")
        self.assertEqual(job["parameters"]["uncertainty_method"], "evidential")
        self.assertEqual(
            job["parameters"]["uncertainty_fields"],
            [
                "aleatoric_variance",
                "epistemic_variance",
                "predictive_variance",
                "aleatoric_std",
                "epistemic_std",
                "predictive_std",
                "lower_95",
                "upper_95",
            ],
        )
        self.assertEqual(plan.metadata["uncertainty_method"], "evidential")

    def test_explicit_image_and_resources_override_environment(self) -> None:
        request = SarJobRequest(
            model_name="rxnresid-v1",
            mode=ModelMode.INFER,
            image="registry.example/explicit:latest",
            code_artifact=Artifact("rxnresid-source", "RxnResid", kind="directory"),
            checkpoint=Artifact("checkpoint", "models/checkpoint.pt"),
            input_directory="RxnResid",
            command=("bash", "run.sh"),
            project_id="explicit-project",
            machine_type="explicit-machine",
        )
        with patch.dict(
            os.environ,
            {
                "BOHR_ML_IMAGE": "registry.example/default:latest",
                "BOHR_PROJECT_ID": "default-project",
                "BOHR_MACHINE_TYPE": "default-machine",
            },
            clear=False,
        ):
            job = request.to_bohr_job()

        self.assertEqual(job.image, "registry.example/explicit:latest")
        self.assertEqual(job.project_id, "explicit-project")
        self.assertEqual(job.machine_type, "explicit-machine")

    def test_named_rxnresid_plugin_keeps_sar_capability_compatibility(self) -> None:
        self.assertIsInstance(RxnResidPlugin(), SarPlugin)
        self.assertEqual(RxnResidPlugin().manifest.plugin_id, "tricycle.sar")

    def test_official_rxnresid_command_builders(self) -> None:
        train = build_training_command(fold_index=0, extra_args=("--epochs", "1"))
        self.assertEqual(train[0], "python3.12")
        self.assertIn("train.py", train)
        self.assertIn("configs/rxnresid.yaml", train)
        self.assertIn("--fold-index", train)

        infer = build_inference_command(
            checkpoint="runs/fold-00/checkpoint.pt",
            data="data/paths.csv",
            output="outputs/predictions.csv",
            cpu=True,
        )
        self.assertIn("predict.py", infer)
        self.assertIn("--checkpoint", infer)
        self.assertIn("runs/fold-00/checkpoint.pt", infer)
        self.assertIn("--cpu", infer)

        finetune = build_finetune_command(
            checkpoint="runs/fold-00/checkpoint.pt",
            output="outputs/refit",
            epochs=10,
            learning_rate_factor=0.2,
        )
        self.assertIn("--refit", finetune)
        self.assertIn("--checkpoint", finetune)
        self.assertIn("--epochs", finetune)
        self.assertNotIn("--config", finetune)
        self.assertNotIn("--fold-index", finetune)
        self.assertEqual(
            build_refit_command(checkpoint="runs/fold-00/checkpoint.pt"),
            build_finetune_command(checkpoint="runs/fold-00/checkpoint.pt"),
        )

        with self.assertRaises(InvalidRequestError):
            build_training_command()
        with self.assertRaises(InvalidRequestError):
            build_finetune_command(checkpoint="", epochs=1)

        with self.assertRaises(InvalidRequestError):
            build_inference_command(checkpoint="", data="data/paths.csv")

    def test_refit_plan_uses_checkpoint_owned_configuration(self) -> None:
        plan = SarPlugin().plan(
            "finetune",
            {
                "model_name": "rxnresid-refit",
                "image": "registry.example/dl:rxnresid",
                "checkpoint": {
                    "artifact_id": "checkpoint",
                    "location": "runs/fold-00/checkpoint.pt",
                },
                "code_artifact": {
                    "artifact_id": "rxnresid-source",
                    "location": "RxnResid",
                    "kind": "directory",
                },
                "input_directory": "RxnResid",
                "command": ["bash", "run.sh"],
            },
            ExecutionContext(run_id="rxnresid-refit"),
        )
        job = plan.parameters["bohr_job"]
        self.assertEqual(job["operation"], "sar.finetune")
        self.assertEqual(job["parameters"]["upstream_protocol"], "refit")

    def test_plan_rejects_a_different_rxnresid_repository(self) -> None:
        with self.assertRaisesRegex(InvalidRequestError, "source_repository"):
            SarPlugin().plan(
                "train",
                {
                    "model_name": "rxnresid-v1",
                    "image": "registry.example/runtime:latest",
                    "dataset": {"artifact_id": "dataset", "location": "data/train.csv"},
                    "code_artifact": {
                        "artifact_id": "rxnresid-source",
                        "location": "RxnResid",
                        "kind": "directory",
                    },
                    "input_directory": "RxnResid",
                    "command": ["bash", "run.sh"],
                    "parameters": {"source_repository": "https://github.com/other/RxnResid"},
                },
                ExecutionContext(run_id="rxnresid-wrong-source"),
            )

    def test_task_command_is_required(self) -> None:
        with self.assertRaises(InvalidRequestError):
            SarPlugin().plan(
                "train",
                {
                    "model_name": "rxnresid-v1",
                    "image": "registry.example/runtime:latest",
                    "dataset": {"artifact_id": "dataset", "location": "data/train.csv"},
                    "code_artifact": {
                        "artifact_id": "rxnresid-source",
                        "location": "RxnResid",
                        "kind": "directory",
                    },
                    "input_directory": "RxnResid",
                },
                ExecutionContext(run_id="sar-command-required"),
            )


if __name__ == "__main__":
    unittest.main()
