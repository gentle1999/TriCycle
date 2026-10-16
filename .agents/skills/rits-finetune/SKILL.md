---
name: rits-finetune
description: "Plan or explicitly submit a RitS fine-tuning job to Bohr from a TOML request."
---

# Fine-Tune RitS

Use upstream `scripts/train.py` with Hydra override `finetune_from=<checkpoint>`. It loads weights and creates a new optimizer, scheduler, and training run from current configuration. This differs from full-state `resume=<checkpoint-or-run-directory>`.

Read `model.repository`/`model.revision` from the [model asset configuration](../../../packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml) and record the matching full commit SHA as `source_revision`, not a movable `main`. Validate default pretrained weights against configured `checkpoint_sha256`; user training checkpoints use their own artifact/run bindings. Do not apply the default weight hash to every checkpoint or put fixed hashes in skills.

Provide source directory, dataset artifact, checkpoint artifact, `input_directory`, worker image, and actual command. Dataset root, output directory, epochs, and similar settings remain native RitS Hydra configuration/overrides.

`tricycle_rits.build_finetune_command(checkpoint=..., overrides=...)` returns native Python/Hydra argv. It neither generates `run.sh` nor installs dependencies. Task preparation must deliver any needed launcher; use `bash run.sh` only if the script actually exists.

Read interpreter, image, and dependency recipes from the selected task configuration/source. Check Torch/CUDA, PyG build dependencies, logging dependencies, and GPU availability using package documentation. Do not copy fixed Python/CUDA versions from skills or replace image Torch ad hoc.

Database-exported mapped reaction/TS XYZ CSV is source data, not a preprocessed training directory. Run the selected revision's preprocessor. Check actual parameters and data semantics before producing required AAM columns, forward/reverse pairs, shared TS XYZ, or exclusion indices. Validate atom maps, elements, and XYZ order; preserve rejection records. Renaming files is not preprocessing.

Review artifacts, `source_repository`, `source_revision`, and command:

```bash
uv run --package tricycle-agent tricycle-agent plan \
  rits.finetune configs/example-rits-finetune-request.toml --run-id rits-finetune-smoke
```

Live submission explicitly uses `run ... --live`. Dry-run reads no credentials, accesses no network, uploads nothing, and submits nothing. Use `bohr.job.status`/`bohr.job.download` for submitted jobs instead of creating duplicates.

## Internal Docker/SSH execution

Use `rits.ssh.finetune` when the user selects internal execution. Bohr scheduling failure alone does not authorize switching executors. SSH uploads `bundle_dir` and manages lifecycle; training must run inside `container_image`, not on the bare host.

The image needs fork-compatible Python/PyTorch/CUDA and system dependencies. The remote Docker daemon and NVIDIA Container Toolkit must be available. GPU selection defaults to `--gpus all`; `container_gpus = "0"` or `"0,1"` selects devices. Query logs through remote Docker-context `logs` and state through container `inspect`. The executor injects `WANDB_MODE=disabled` and `WANDB_DISABLED=true`.

Use configured local OpenSSH agent/config, never private keys/passwords in requests. The target must already be in `known_hosts`, with noninteractive authentication and strict host-key checking. Dry-run reads no SSH configuration/keys, accesses no Docker, and makes no network calls.

```bash
uv run --package tricycle-agent tricycle-agent plan \
  rits.ssh.finetune configs/example-rits-ssh-finetune-request.toml \
  --run-id rits-internal-smoke

uv run --package tricycle-agent tricycle-agent run \
  rits.ssh.finetune configs/example-rits-ssh-finetune-request.toml \
  --run-id rits-internal-smoke --live
```

Manage the same `job_id` with `rits.ssh.status`, `rits.ssh.logs`, and `rits.ssh.download`. `rits.ssh.terminate` sends Docker stop/SIGTERM and requires explicit `--confirm`. Download only selected relative output paths, not the full code/data bundle. See [rits-ssh-finetune](../rits-ssh-finetune/SKILL.md) for requests and lifecycle details.
