---
name: dpa-berny-prepare
description: "Prepare local DPA-Berny or Gaussian worker inputs from a TOML request without remote submission."
---

# Prepare DPA-Berny/Gaussian Inputs

Use `xyz_patterns`, `workers`, `engine`, and either DPA `model_path` or Gaussian `functional` / `basis`; the output field is `out_dir`. Unknown fields and old aliases are errors.

`engine="dpa"` requires GPU `machine_type`; `engine="gaussian"` requires CPU. Check the explicit configuration or actual `BOHR_MACHINE_TYPE`, without overriding resources through extra CLI arguments. DPA-Berny energy/gradients come from a GPU model service, so a GPU node is required even when the Gaussian optimizer uses CPU.

```bash
uv run --package tricycle-agent tricycle-agent run \
  dpa-berny.prepare request.toml --run-id <id> --run-dir 'runs/<date>/<timestamp>/<id>'
```

This creates local inputs and `batch.toml` only. Review `.gjf`, `tasks.tsv`, and `submit-plan.tsv` before `bohr.batch.submit`.

DPA smoke acceptance checks CUDA evidence in `results/dpa-device.json`, actual service GPU use, energy/gradient requests, and optimization results. CPU completion is not a pass. Investigate environment/node failures after CUDA preflight; do not rerun on CPU to pass. See the [package guide](../../../packages/tricycle-bohrsubmit/README.md).

## Failure recovery and boundaries

This capability neither submits remotely nor modifies existing runs. Correct invalid XYZ, engine, or model parameters in TOML, then prepare a new empty `run-dir`; preserve failed directories for diagnosis.

New worker inputs are prepared temporarily and published to shared asset storage. Runs retain input references and records, validated through `batch.toml.input_assets` and worker `input-assets.json`. The adapter must materialize inputs temporarily for submission; never upload the worker record directory directly.
