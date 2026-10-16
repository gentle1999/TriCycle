---
name: dpa-berny-rescue
description: "Prepare a DPA-Berny or Gaussian rescue run from an existing run directory using a TOML request."
---

# Prepare a DPA-Berny Rescue Run

Use existing batch results and failure details to skip successful tasks and retry failures under the selected policy.

## Inputs and outputs

A TOML request supplies `source_run`, worker count, optional download directory, and retry policy. Outputs are a new local run, `rescue-summary.tsv`, `batch.toml`, and submit-ready tasks. No remote resubmission occurs automatically.

```bash
uv run --package tricycle-agent tricycle-agent run \
  dpa-berny.rescue request.toml --run-id <id> --run-dir 'runs/<date>/<timestamp>/<id>'
```

Review the rescue plan and skipped successes before `bohr.batch.submit`. Preserve the source directory. Rescue also requires GPU for DPA and CPU for pure Gaussian; invalid source resource settings are rejected.

The new run generates Bash workers from the current package implementation and resolves task/model assets from source `batch.toml.input_assets`. It prepares inputs temporarily, stores them by content, and retains references without copying old launch scripts. Existing physical input directories remain readable. Create a new run from a corrected request and preserve source records. DPA CPU results cannot serve as passing GPU smoke evidence.

## Failure recovery

If planning fails, inspect source data and failure details, correct TOML, and create another new rescue run. Do not overwrite the source or resubmit remote workers through this skill.
