---
name: rits-infer
description: "Plan or explicitly submit a RitS inference job to Bohr from a TOML request."
---

# Run RitS Inference

`rits.infer` plans native TS candidate inference using `tricycle_rits.build_inference_command` and the asset configuration's `inference_entrypoint`. For complete profiles with filtering, clustering, and energy selection, use [reactionopt-profile](../reactionopt-profile/SKILL.md) and `build_ts_pipeline_command`. Raw candidates are not final profiles. Training uses `rits-finetune` or `rits-train`.

`request.toml` is the TriCycle request; native RitS configuration such as Hydra YAML remains an uploaded input artifact with source, checkpoint, and data. The worker executes `command`; a `bash run.sh` script must already be delivered. Follow the selected image/task installation recipe instead of maintaining another pip recipe in skills.

Provide `model_name`, `image`, `code_artifact`, checkpoint artifact, `input_directory`, actual `command`, and inference parameters. The uploaded directory normally contains source at the configured revision, checkpoint, native YAML, and input data. For profiles, call `scripts/run_ts_pipeline.py`, write one mapped reaction into the `--reactions` file, and preserve `selected_xyz`, `summary.json`, cluster summaries, and energy rankings. This script is a fork-maintained pipeline, not an official RitS release entry point; `scripts/sample_transition_state.py` is only candidate generation.

Review target/inputs with dry-run before authorized `--live` submission. Follow `run.toml.resume.next_action` for status, download, and recovery, not chat memory.

Read `model.repository`/`model.revision` from the [model asset configuration](../../../packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml). Cache weights at `checkpoint_path`, stage at `task_checkpoint_path`, and validate `checkpoint_sha256`. Checkpoint artifacts reference task-local paths. Custom trained checkpoints use their actual artifact/run bindings, not the default pretrained hash. Skills do not bind concrete commits/hashes.

The profile pipeline defaults to mapped-stereo filtering, heavy-atom complete-linkage Kabsch RMSD clustering, medoid preselection, and configured g-xTB `xtb --gxtb --sp` single-point energies for retained cluster members. Supply g-xTB in the worker environment or explicitly via pipeline arguments. Ordinary GFN1/GFN2-xTB is not a substitute.

## Failure recovery

Complete missing checkpoint/image/stable-run fields before submission. Inspect after Bohr submission/query failures; use `resume --live` only after confirming no job exists. Query existing jobs through `bohr.job.status`.
