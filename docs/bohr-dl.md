# Bohr model environments

## Configuration sources

Configuration selects model environments and platform resources. Do not infer them from installation examples in general documentation. Current entry points are:

| Workflow | Configuration and preparation |
| --- | --- |
| SAR models, single RxnResid jobs, and sampling | [catalog.json](../packages/tricycle-sar/src/tricycle_sar/backends/catalog.json), [environment configurations](../packages/tricycle-sar/src/tricycle_sar/backends/environments/), [SAR package](../packages/tricycle-sar/README.md) |
| RitS training/fine-tuning/inference | [Model assets](../packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml), [example request](../configs/example-rits-train-request.toml), [RitS package](../packages/tricycle-rits/README.md) |
| DPA-Berny and Gaussian | [Engine request example](../configs/example-dpa-berny-request.toml), [BohrSubmit](../packages/tricycle-bohrsubmit/README.md) |
| Sampling suites and full trajectories | [Configuration index](../configs/README.md); model delivery uses shared SAR interfaces |

Bohr GPU requests use the unified machine type in current configuration. DPA energies and gradients must run on GPUs; pure Gaussian and fingerprint models use CPUs. RXNGraphormer uses the internal SSH/Docker executor selected by the catalog. Global machine defaults must not override these resource boundaries.

## Interpreters, environments, and source

SAR `workers/unified.sh` enters the shared executor. The environment engine selects an interpreter and setup procedure from the chosen profile. Image-inheriting environments validate base Python/Torch and then create task environments that inherit image dependencies; CPU environments isolate dependencies according to their recipes. Actual Python paths, Torch/CUDA versions, and dependencies come from execution records. Do not assume `python3.12` or a system path is the model interpreter.

Preparation delivers source, configuration, data, and checkpoints; an image does not replace source bindings. RitS asset TOML specifies model source and weights. Fine-tuning uses `finetune_from`; recovery that retains optimizer state uses `resume`. ReactionOpt TS pipelines additionally require a request binding to source containing `scripts/run_ts_pipeline.py`; a model-asset revision does not prove pipeline provenance.

Fixed assets must exist locally and pass validation. ReactionT5 weights, tokenizer, and configuration accompany the task; MolCLR uses its specified pretrained weights. Preserve diagnostics on failure instead of upgrading dependencies, swapping checkpoints, or changing training methods to pass checks.

## Local preparation and device diagnostics

Package entry points prepare read-only image inventories and SAR environment tasks:

```bash
uv run --all-packages python -m tricycle_sar.preparation.inventory \
  --config configs/example-sar-inventory.toml --out-dir /path/to/new-inventory
uv run --all-packages python -m tricycle_sar.preparation.environment \
  --model rxnresid --out-dir /path/to/new-environment-check \
  --group-config /path/to/group.toml
```

These commands prepare local inputs only. Generated Bohr requests then follow [batch prepare/submit/status/download](../packages/tricycle-bohrsubmit/README.md#cli-examples). See [example-sar-bohr-group.toml](../configs/example-sar-bohr-group.toml) for group configuration. Submission explicitly enters the live path within existing authorization.

Validate GPU setup inside the actual task container and model interpreter:

- Python/Torch paths, CUDA runtime, and driver/device information from `nvidia-smi`;
- driver API version and the CUDA dynamic libraries actually loaded;
- real CUDA operations and device evidence from the model process.

A driver observed in node mode does not prove another job's environment. Allocating a GPU or obtaining `torch.cuda.is_available() == True` does not prove GPU model execution. See [accelerator execution](sar-accelerator-execution.md) for SAR preflight logs and failure actions. Repeatedly submitting until a compatible node happens to appear is not compatibility acceptance.

## Smoke acceptance and recovery

Report input preparation, environment checks, model forward/backward probes, post-training prediction, and complete workflow results separately. Probe success does not replace full training/inference acceptance; RitS inference success does not replace ReactionOpt TS post-processing acceptance.

After completion, query status, download artifacts, and verify expected predictions/structures, numerical validity, input coverage, and GPU evidence. For DPA, verify energy/gradient requests and optimization artifacts as well; accept pure Gaussian as a CPU task. Actual job IDs, driver versions, durations, and success/failure records belong only in the run, outside general guides.

Environment setup retries transient network errors within bounded limits. Inspect deterministic dependency, asset, or CUDA failures first. Training and inference do not automatically restart in full, and unknown submission outcomes require receipt reconciliation. Computation authorization does not authorize protocol changes. Retry details belong to the [SAR package](../packages/tricycle-sar/README.md#compact-job-outputs) and configuration.
