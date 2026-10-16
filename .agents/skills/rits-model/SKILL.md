---
name: rits-model
description: "Create RitS training, fine-tuning, or inference jobs and submit them to Bohr using the shared job contract."
---

# RitS Training, Fine-Tuning, and Inference

Use to train/fine-tune RitS or infer reaction candidates from a checkpoint.

## Inputs

Model asset configuration requires the current `schema` and `[model]` table and accepts only declared model fields. See the package README; flattened formats and unknown fields are errors.

- `model_name`, `image`, `code_artifact`, `input_directory`, and optional Bohr resources.
- Dataset artifact for training; checkpoint artifact for inference.
- Mode `train`, `finetune`, or `infer`, with an actual task `command`.
- Official training uses `scripts/train.py`. ReactionOpt profiles use fork-added `scripts/run_ts_pipeline.py`, which calls `scripts/sample_transition_state.py` for candidate generation. Use `build_inference_command` for native inference and `build_ts_pipeline_command` for profiles; they are not interchangeable.
- TriCycle requests use TOML. Model-native configuration is an input artifact in the RitS-required format. `parameters` records only versions, paths, hashes, and orchestration metadata.
- Read `model.repository`, `model.revision`, and `model.checkpoint_sha256` from the [model asset configuration](../../../packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml), not fixed skill values. The profile pipeline must come from a fork containing its script.
- Use configured `checkpoint_path` for local cache and `task_checkpoint_path` for uploaded weights.
- `finetune_from=<checkpoint>` loads weights into a new optimizer/scheduler. `resume=<checkpoint-or-directory>` restores optimizer, scheduler, epoch, and step; do not confuse them.

## Workflow

1. Confirm dataset version, task type, split, and seed.
2. Call `RitsPlugin.plan("train", ...)`, `plan("finetune", ...)`, or `plan("infer", ...)`.
3. Review generated `rits.train`/`rits.finetune`/`rits.infer` Bohr jobs and artifact directions.
4. Call `submit_to_bohr` only with explicit authorization and save the external job ID.
5. Preserve model artifacts, predictions, and actual input bindings. Database ingestion is a separate workflow, never an automatic consequence of training.

## Outputs and constraints

Jobs use `BohrJobSpec`. Shared artifact, TaskPlan, and submission logic belongs to `tricycle-bohr`'s `BohrModelJobRequest`, `BohrModelPlugin`, and `BohrJobExecutor`; RitS owns domain manifests, requests, and command builders. `TaskPlan.metadata.bohr_target` must be `true`.

Default execution is dry-run, without model-service or Bohr calls. Workers use the prepared deep-learning image and system Python, without creating virtual environments. RitS YAML/Hydra configuration is task input, not TriCycle metaproject configuration. Fine-tuning uses upstream `finetune_from` with both dataset and checkpoint artifacts, never the inference entry point. Read input/command/checkpoint contracts from current assets, builders, and source; stop preparation when required inputs are missing instead of inventing placeholder specifications.

Implementation: `packages/tricycle-rits/src/tricycle_rits/plugin.py`.
