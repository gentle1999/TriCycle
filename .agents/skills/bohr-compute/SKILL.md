---
name: bohr-compute
description: "Plan and execute a computational chemistry task on the Bohr platform through an injected gateway."
---

# Bohr Compute

Use this workflow to turn a reaction configuration, structure file, or model inference request into a Bohr job. Generate a `TaskPlan` and review input artifacts, image, resources, and operation before submission.

## Inputs

- `job_name`: stable job name.
- `operation`: defined by the plugin contract, for example `reaction.single-point`.
- `image`, `command`, optional `project_id` and `machine_type`.
- `input_artifacts`, `output_artifacts`, serializable `parameters`.
- `run_id` and `dry_run` in the run context.

## Workflow

1. Validate with `BohrComputeRequest`.
2. Call `BohrComputePlugin.plan("compute", request, context)`.
3. Check `TaskPlan.parameters["job"]` and input completeness. DPA requires a GPU node and GPU acceleration; pure Gaussian requires a CPU node.
4. Call `submit` only with explicit user/orchestrator authorization and an injected `BohrGateway`.
5. Save the external job ID; query status and download artifacts through the gateway.

## Outputs and boundaries

Planning produces a JSON-serializable `TaskPlan`; submission produces `TaskResult(status=submitted, external_id=...)`. The default `dry_run=True` must not read credentials or access the network. Image commands, database fields, and retry policy belong to adapter configuration; do not guess them here.

Implementation: `packages/tricycle-bohr/src/tricycle_bohr/plugin.py`.
