---
name: sar-train
description: Plan or explicitly submit a SAR training job with current backends and compact experiment records.
---

# Train SAR Models

Follow [SAR experiment records and archiving](../../../packages/tricycle-sar/README.md#experiment-records-and-archiving); use [sar-benchmark](../sar-benchmark/SKILL.md) for five-fold comparisons. Define shared data/protocol/parameters once, recording only fold/seed/round differences. Do not add per-task manifests or installation recipes.

Ordinary RxnResid training uses native `train.py` and `--fold-index`; selection-checkpoint refit uses [sar-finetune](../sar-finetune/SKILL.md). Read `model.revision` from [model assets](../../../packages/tricycle-sar/src/tricycle_sar/assets/rxnresid-model.toml), and image/initialization/hashes from current backend configuration. Reuse validated workers; do not put fixed versions/hashes in skills, reinstall Torch/CUDA, or rebuild virtual environments unilaterally.

TriCycle requests use TOML; native model configuration/data are input artifacts. Plan locally:

```bash
uv run --package tricycle-agent tricycle-agent plan sar.train request.toml --run-id <id>
```

With existing training authorization, execute `run sar.train ... --live` through an injected Bohr adapter. Store source/uncertainty definitions only in job parameters, not duplicate metadata. Inputs must be complete with matching versions/hashes. Share the fixed split within an experiment; changed data requires a new run using the current method.

Keep receipts, logs, checkpoints, and recovery state in `runs/`. Publish through scientific-file export interfaces, not whole worker copies. Query/download existing job IDs; failure reruns use new runs without overwriting successful/running jobs. Dry-run reads no credentials, makes no network calls, trains nothing, and submits nothing.

## Shared execution contract

Use the `tricycle_sar` environment and runners, without `tools/` compatibility entry points or a separate protocols source tree. Workers consolidate predictions/metrics/status; retain checkpoints explicitly when needed, including every model required by later train/refit/infer stages.

New tasks bind shared environment/entry points through `backends/catalog.json`; common core files are delivered with identical hashes. Maintain only current catalog/source/methods, without historical implementation copies or compatibility variants. Result method IDs record provenance. `results verify` independently recomputes scientific results; `results prepare` builds new inputs with the current backend and neither trains nor submits.

Generic RxnResid train/refit/infer uses request-supplied native configuration, CSV, and checkpoint independently of data origin; reaction-db is an optional adapter. Sampling/five-fold column names, grouping, and route aggregation belong to experiment protocols, not generic model constraints.

`sar.train/infer/finetune` shares command builders, with image/machine matched to the backend. Local source inputs must match the fixed archive. Planning records input hashes and submission rechecks them. `single_job.py` and `sampling_runtime` share initialization/source. A dry-run plan with missing inputs cannot be submitted directly. Cumulative-label training uses from-scratch mode on the full train split; prediction coverage is checked by route identity.
