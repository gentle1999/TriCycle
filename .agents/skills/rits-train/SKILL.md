---
name: rits-train
description: "Plan or explicitly submit a RitS training job to Bohr from a TOML request."
---

# Train RitS

Ordinary training has no checkpoint override. Use `rits-finetune` to start from existing weights and `resume=<path-or-run-directory>` for interrupted-training recovery.

`request.toml` is the TriCycle task request. Native RitS configuration, such as Hydra YAML, remains an uploaded input artifact alongside source/data; do not convert it to TriCycle TOML. The worker executes `command`; `bash run.sh` is valid only when the script is delivered in `input_directory`. Use the selected task/image recipe for interpreter, dependency initialization, and source installation, without ad hoc pip recipes copied from skills.

The request includes `model_name`, `image`, `code_artifact`, dataset artifact, `input_directory`, actual `command`, and training parameters. The uploaded local task directory normally contains source at the revision selected by current RitS configuration, data, and native configuration. Commands must execute directly there. Official training uses `scripts/train.py`; `build_training_command` builds the base command.

Read `model.repository` and `model.revision` from the [model asset configuration](../../../packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml) and record the full revision in requests. For pretrained starts, deliver weights at configured `checkpoint_path`/`task_checkpoint_path`, validate `checkpoint_sha256`, and use task-local paths in native parameters.

```bash
uv run --package tricycle-agent tricycle-agent plan rits.train request.toml --run-id <id>
```

Remote submission needs user authorization and `--live`. Credentials come from environment variables or automatically discovered project `.env`; public job ID and follow-up action go in `run.toml`.

## Failure recovery and routing

Correct missing dataset/image/training parameters in TOML and replan. After submission errors, inspect state and public job ID; resume only after confirming no submission occurred. Use `bohr.job.status` for existing jobs rather than creating duplicates.
