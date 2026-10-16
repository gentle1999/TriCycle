---
name: bohr-compute-submit
description: "Submit one planned Bohr computational chemistry job through the Bohr adapter after explicit approval."
---

# Submit a Bohr Job

First use `bohr.compute.plan` to review the image, command, resources, and input artifacts. DPA requires GPU acceleration on a GPU node; pure Gaussian requires a CPU node. Do not override that choice with extra CLI flags. Once authorized:

```bash
uv run --package tricycle-agent tricycle-agent run \
  bohr.compute.submit request.toml --run-id <id> --live
```

Only `--live` constructs the Bohr CLI adapter and reads `BOHR_ACCESS_KEY` from the environment or automatically discovered project `.env`. Shell variables take precedence. Record the public job ID in `run.toml` and follow `resume.next_action` for status. Never write tokens into requests, state, or logs.

## Outputs and boundaries

Declare a nonempty `output_artifacts` list or `parameters.backward_files` before
submission. Select named output directories/files, never `.` or `*`. The adapter
checks input trees and ZIP/tar members for installed environments and caches before
upload. Prepare clean source/data/weight bundles without changing the scientific
environment; do not bypass these checks with extra CLI arguments.

Outputs are the public job ID and `run.toml` state. This capability submits; polling and downloading use separate capabilities. Default dry-run reads no credentials and makes no network calls; only `--live` writes remotely.

## Failure recovery

Check whether `run.toml` already contains a public job ID, then inspect Bohr environment configuration and adapter logs. Retry with `resume --live` only after confirming no remote job was created. Otherwise query `bohr.job.status` first to avoid duplicate submission.
