---
name: rits-ssh-finetune
description: "Stage and execute a RitS fine-tuning bundle in a Docker container on an internal GPU host over SSH."
---

# Fine-Tune RitS on Internal Docker/SSH

Use `rits.ssh.finetune` when the user selects internal Docker/SSH execution. Local OpenSSH/scp uploads the bundle; the remote Docker daemon starts a GPU container. Training never runs on the bare SSH host. Mount the remote job directory at `/workspace/job` (or `container_workdir`). Docker's logging driver retains stdout/stderr for `rits.ssh.logs`. Use non-login `bash -c` to preserve image environment such as PATH; do not depend on login-shell initialization.

All SSH lifecycle requests accept only fields declared by `RitsSshFinetuneRequest`; unknown fields are errors. Fine-tuning requires `host`, `remote_root`, `bundle_dir`, `command`, and `container_image`. `container_gpus` defaults to `"all"` and also accepts selections such as `"0"` or `"0,1"`.

Before any SSH/Docker side effect, the adapter rejects installed environments and
package/tool caches in `bundle_dir`, including ZIP/tar members and renamed venvs
identified by `pyvenv.cfg`. Stage clean source, dependency declarations, data, and
checkpoints. Use the selected image/environment procedure; never change scientific
dependencies or strip required model inputs merely to bypass transfer validation.

The image must be accessible to the remote daemon and compatible with its NVIDIA driver/Container Toolkit. It must provide Bash, Python, fork dependencies, and compatible PyTorch/CUDA. Before submission the adapter checks `docker info` through the Docker context; it does not guess, replace, or implicitly build the selected image. Containers set `WANDB_MODE=disabled` and `WANDB_DISABLED=true`.

The remote SSH user needs Docker socket access, usually through the docker group or rootless Docker. Local Docker CLI sends requests through the remote context.

## Prepare the training image

Use the [Dockerfile](../../../packages/tricycle-rits/docker/cu130/Dockerfile) and adjacent dependency manifest as the source of image/Torch/CUDA/dependency versions. A repository recipe does not prove the image exists on the host. Read `container_image`, `docker_context`, host, and GPU selection from TOML.

Image building is a separate operation, performed only when included in the task. Ordinary fine-tuning does not implicitly build or replace an image. Build using the configured context/tag and the recipe directory as build context; source, data, and checkpoints still come from the bundle. A supporting bundle installer consumes `RITS_DEPS_IN_IMAGE=1`; the SSH adapter does not install model dependencies. Validate interpreter/key dependencies after building and GPU access after starting the actual container.

Authentication uses existing local OpenSSH agent/config. Keep passwords/private keys out of TOML. Pre-register host keys in `known_hosts` and configure `BatchMode=yes` and `StrictHostKeyChecking=yes`. On first live use, the adapter checks `docker context ls` and, if absent, creates the context with `docker context create remote --docker host=ssh://user@host`. An existing same-name context with a different endpoint is an error, never overwritten. Planning does not inspect keys or access SSH/Docker/network. Upload/start occurs only with explicit `run ... --live`.

```bash
uv run --package tricycle-agent tricycle-agent plan \
  rits.ssh.finetune configs/example-rits-ssh-finetune-request.toml \
  --run-id rits-internal-smoke

uv run --package tricycle-agent tricycle-agent run \
  rits.ssh.finetune configs/example-rits-ssh-finetune-request.toml \
  --run-id rits-internal-smoke --live
```

Submission returns `external_id` (remote `job_id`). Manage with `rits.ssh.status`, `rits.ssh.logs`, `rits.ssh.terminate`, and `rits.ssh.download`. Stopping uses `docker stop` and requires `--confirm`. Downloads accept safe relative `download_paths` and local `download_dir`; retrieve required checkpoints/logs only, not the full data bundle.

## Long-running monitoring and recovery

Successful submission means the container started, not that training resumed or progressed. Combine status/logs on the first check: runtime/GPU validation passed, checkpoint restored, epoch/step exceeded checkpoint values, and finite train loss keeps updating. Preprocessing may temporarily produce no epoch logs; silence alone does not justify restart. Status tracks container lifecycle; logs track training progress.

Use an existing supervisor or user-authorized monitor, avoiding multiple controllers for one job. Preserve job ID, request, and run directory. If model-assisted monitoring is used, the user or supervision configuration selects model/reasoning settings, not fixed skill values.

After nonzero exit, inspect the final traceback and verify state is `exited` before retrying. Prefer the job's latest complete Lightning checkpoint, such as `outputs/checkpoints/last.ckpt`, and verify `loops`, `epoch`, and `global_step`. A model callback checkpoint containing only `state_dict` restores weights, not necessarily Trainer progress. Otherwise use a user-provided or validated full recovery point.

Each retry uses a new bundle/run/job ID and preserves previous bundles, logs, and checkpoints. Ensure no other container for the same training task remains running. After restart, verify restored progress, `Restored all states`, and subsequent epoch/loss updates. Do not change batch size, epochs, optimizer, or training data merely because of a restart.

Automatically retry the same error signature at most three times. Repeated failure, no verifiable recovery point, or suspected hang requires stopping automatic retries and reporting to the user. Stopping a running container still requires explicit confirmation under `rits-ssh-terminate`. Restart authorization does not authorize cleaning/deleting old task directories.

## Lifecycle requests

Example status/log TOML:

```toml
host = "gpu-box.internal"
user = "trainer"
remote_root = "/mnt/fast/rits-jobs"
job_id = "rits-internal-smoke-generated-suffix"
log_lines = 200
```

Termination also requires `host`, `remote_root`, `job_id`:

```bash
uv run --package tricycle-agent tricycle-agent run \
  rits.ssh.terminate request.toml --live --confirm
```

Downloads require `download_paths = ["outputs"]` (adjust to actual training outputs) and `download_dir`. Exclude large directories containing training data.
