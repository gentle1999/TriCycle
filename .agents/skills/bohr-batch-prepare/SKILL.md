---
name: bohr-batch-prepare
description: "Prepare an auditable local Bohr batch run from a TOML request without submitting remote jobs."
---

# Prepare a Bohr Batch

Use to expand task files, allocate workers, and generate `batch.toml` and `submit-plan.tsv`.

## Inputs and outputs

The TOML selects either `task_patterns` globs or `task_defaults` + `task_specs`, together with worker count, script, project, machine, and image. Preparation creates `run.toml`, `request.toml`, `batch.toml`, worker records, and a submission plan in an empty directory. It writes local files only, without Bohr credentials or network access.

```bash
uv run --package tricycle-agent tricycle-agent run \
  bohr.batch.prepare request.toml --run-id <id>
uv run --package tricycle-agent tricycle-agent inspect --run-dir 'runs/<date>/<timestamp>/<id>'
```

Check task count, worker partitioning, and `submit-plan.tsv` before submission. This skill neither submits nor terminates remote jobs.

Use clean input directories: installed environments (`.venv`, renamed venvs with
`pyvenv.cfg`, Conda environments) and package/tool caches fail transfer validation,
including when present in ZIP/tar inputs. Keep source, dependency declarations,
scripts, data, and required weights. Correct the bundle named in the error; do not
silently change dependencies or edit frozen input hashes.

## Failure recovery and routing

Continue an existing prepared run with `bohr.batch.submit` or `resume`; do not prepare it again. If validation fails or the directory is nonempty, correct the TOML and choose a new `run-id` and empty directory. This local stage does not automatically recover old directories.

## Batches bound to a SAR protocol

Requests referencing a current method generate one run-level `training-protocol.json`, binding shared inputs and per-worker task hashes; `batch.toml` records its SHA256. Worker inputs are built temporarily and stored by content in the project asset store. `batch.toml.input_assets` and each worker's `input-assets.json` bind complete inputs; runs retain neither input copies nor symlinks. Hard links are an optional internal asset-store optimization. Submission rejects changed bindings and accepts only the current run-level marker and bindings, not historical per-worker formats. New jobs use the current package backend configuration.

Prefer inline task tables when parameters are shared: write common fields once and only unique `name`, fold/seed, and other differences per row. Rows cannot override common fields. Preparation expands worker TOML inputs and balances partitions in table order; do not create redundant source TOMLs or rename files to rebalance workers.
