---
name: bohr-submit
description: "Prepare and operate typed Bohr batch, Gaussian, and DPA-Berny workflows through an injected gateway."
---

# Bohr Batch and Chemistry Workflows

## Current Bohr CLI

The plugin uses the current `bohr` CLI 2.x interface: public `BOHR_ID` (JSON `bohrId`) for lifecycle operations, `--output json` for machine-readable output, and `--out` for downloads/logs. Platform-internal `id` values are invalid lifecycle IDs. Migrated runs containing `bohr-id-map.tsv` are rejected; create a new request with public IDs. The adapter does not map internal IDs automatically.

The CLI has no `--backward_files` flag, but its `--config_file` JSON schema supports `backward_files: ["relative/path", "glob/**"]`, corresponding to submitted API `outFiles`. For `output_artifacts` or explicit `parameters.backward_files`, the gateway creates a temporary JSON configuration containing only this allowlist and deletes it after submission. With no result paths declared, Bohr's default full-return behavior remains. `result_path` is the destination for automatic result downloads to Bohrium `/personal` or `/share`, not a result allowlist. Temporary configuration contains no credentials.

Authentication uses process environment `BOHR_ACCESS_KEY`; tools automatically discover `.env` in the working directory or its parents:

```bash
export BOHR_ACCESS_KEY='...'
bohr auth status --verify
```

`BOHR_PROJECT_ID`, `BOHR_MACHINE_TYPE`, and `BOHR_IMAGE` supply defaults for missing request fields; they are not credentials. Place remote dataset inputs in `input_directory` first or inject a gateway supporting that capability. The current CLI gateway rejects unrepresentable dataset paths. Declare result selection with `output_artifacts` or `parameters.backward_files`.

## Scope and inputs

Use to prepare task files or xyz structures as Bohr workers, or manage submission, status, download, monitoring, and rescue of prepared runs.

Adapter policy TOML uses `[bohr]` and `[retry]`; see the [configuration example](../../../configs/example-bohr.toml). Omitted fields use defaults; flattened fields, unknown tables, and unknown fields are errors.

- Generic batch: `task_patterns`, `workers`, `script` (or `worker_script`), image, project, and machine.
- DPA-Berny/Gaussian: `xyz_patterns`, `workers`, `engine`, Gaussian parameters, and `model_path` only for `engine=dpa`.
- DPA requires GPU `machine_type` and GPU acceleration; pure Gaussian requires CPU. Preparation, submission, and rescue validate resources. No DPA CPU fallback is allowed; acceptance needs device and actual-computation evidence.
- Remote lifecycle: generated `run_dir` and explicitly injected `BohrWorkflowGateway`.
- Resolve image/project/machine from requests and supported environment defaults; review the final plan instead of copying fixed tags from skills.

## Workflow

1. Validate with `BohrSubmitPlugin.plan("batch.prepare", ...)` or `plan("dpa-berny.prepare", ...)`, then `prepare` locally.
2. Review `batch.toml`, `tasks.tsv`, `submit-plan.tsv`, and worker inputs. Preparation does not contact Bohr.
3. Call `submit_prepared` only with an explicitly injected non-dry-run gateway.
4. Maintain runs with `status`, `download`, and `watch`. Download complete worker results before `rescue` for long-tail calculations.
5. Constrained xyz inputs generate fixed-opt + Link1; `multi_stage_chk` generates numbered checkpoints.

For existing runs, inspect before CLI `resume`: it executes `resume.next_action`, not a fixed status/download operation. Resume only when that action matches the intended operation. Do not initialize existing `run.toml` directories with `run`. Use the package API with an injected gateway for independently selected lifecycle actions. Download reports live at `downloads/download-results.tsv`.

## ORCA boundary

No ORCA input schema is defined. Do not invent routes or templates. Use generic `batch.prepare` with an explicit `script` invoking the image's `orca`; add a dedicated planner only once the task schema is defined.

## Outputs and constraints

Outputs are auditable `TaskPlan` objects, run directories, and TSV lifecycle reports. Declarative requests and run configuration use TOML; worker `dpa-berny.env` is a Bash process-environment bridge, not configuration. Default `dry_run=True` reads no credentials, makes no network calls, and submits nothing. Only live-gateway construction loads `.env` authentication. The gateway is the sole `bohr` CLI boundary; plugins must not invoke subprocess directly. Never write tokens into TOML, environment projections, plans, or logs.

Implementation: `packages/tricycle-bohrsubmit/src/tricycle_bohrsubmit/`.

For folds/seeds sharing parameters, prefer `task_defaults` + `task_specs` in one request, mutually exclusive with `task_patterns`. Each row contains a unique `name` and differing fields only; rows cannot override common parameters. Staging expands and balances worker inputs while retaining input/protocol hash validation.
