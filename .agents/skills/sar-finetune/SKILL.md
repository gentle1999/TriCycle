---
name: sar-finetune
description: Plan or explicitly submit an RxnResid checkpoint refit with compact records.
---

# Refit RxnResid

Follow [SAR experiment records and archiving](../../../packages/tricycle-sar/README.md#experiment-records-and-archiving). Define common model/data/protocol once; each round records seed/round, previous-checkpoint reference, changed inputs, and outputs. Do not duplicate environments/protocols/data manifests or parallel summaries. Execution evidence stays in `runs/`.

`sar.finetune` is `train.py --refit --checkpoint <selection-checkpoint>`, retaining the checkpoint's config/fold/split/model settings with a new optimizer. `tricycle_sar.build_finetune_command` validates allowed parameters. Do not also pass `--config`, `--fold-index`, `--split-manifest`, `--seed`, or model/encoder overrides. Provide checkpoint, source artifact, input directory, and actual command; declare additional data as input artifacts when needed.

Read `model.revision` from [model assets](../../../packages/tricycle-sar/src/tricycle_sar/assets/rxnresid-model.toml), and environment/hashes from current backend configuration. Skills do not pin versions/hashes or provide another pip/Torch/CUDA recipe. Keep source and route vocabulary consistent within a trajectory; reject mixed checkpoint versions.

```bash
uv run --package tricycle-agent tricycle-agent plan sar.finetune request.toml --run-id <id>
```

With existing authorization use adapter `run ... --live`. Default execution does not connect, install, train, or submit. Manage submitted jobs through status/download, without blind resubmission. After completion use native `predict.py` for uncertainty output; preparation/model loading alone is not completed training.

Sampling refit reuses `get_backend("rxnresid").refit_command`. For cumulative data changes, `tricycle_sar.rxnresid.prepare_refit_checkpoint` updates data references and recomputes target normalization in a checkpoint copy without modifying original weights. Later rounds must match the same `sar_backend` binding.

## Shared execution contract

Use the `tricycle_sar` environment and runners, without `tools/` compatibility entry points or a separate protocols source tree. Workers consolidate predictions/metrics/status; retain checkpoints explicitly when needed, including every model required by later train/refit/infer stages.

New tasks bind shared environment/entry points through `backends/catalog.json`; common core files are delivered with identical hashes. Maintain only current catalog/source/methods, without historical implementation copies or compatibility variants. Result method IDs record provenance. `results verify` independently recomputes scientific results; `results prepare` builds new inputs with the current backend and neither trains nor submits.

Generic RxnResid train/refit/infer uses request-supplied native configuration, CSV, and checkpoint independently of data origin; reaction-db is an optional adapter. Sampling/five-fold column names, grouping, and route aggregation belong to experiment protocols, not generic model constraints.

`sar.train/infer/finetune` shares command builders, with image/machine matched to the backend. Local source inputs must match the fixed archive. Planning records input hashes and submission rechecks them. `single_job.py` and `sampling_runtime` share initialization/source. A dry-run plan with missing inputs cannot be submitted directly. Cumulative-label training uses from-scratch mode on the full train split; prediction coverage is checked by route identity.
