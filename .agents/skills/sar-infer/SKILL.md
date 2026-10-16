---
name: sar-infer
description: Plan or explicitly submit RxnResid inference with the current backend using shared inputs and compact records.
---

# Run SAR Inference

Follow [SAR experiment records and archiving](../../../packages/tricycle-sar/README.md#experiment-records-and-archiving): reference source/checkpoint/data/prediction protocol once and record only task differences/outputs. Logs, receipts, and device evidence stay in `runs/`, outside formal result packages and without round-by-round duplication.

Use native `predict.py` with checkpoint/data artifacts; training/refit use their dedicated skills. Source revision comes from model assets. Reuse the validated image/installer instead of rewriting environment recipes. `tricycle_sar.build_inference_command` constructs commands.

```bash
uv run --package tricycle-agent tricycle-agent plan sar.infer request.toml --run-id <id>
```

Default execution plans only. With existing authorization use `--live` through the Bohr adapter. Query/download existing jobs instead of blindly resubmitting.

Prediction CSV includes `aleatoric_variance`, `epistemic_variance`, `predictive_variance`, corresponding standard deviations, and 95% intervals. Validate ID coverage and checkpoint-consistent route vocabulary before handing results to sampling. `agent-authored-acquisition` uses its frozen SAR worker to generate `candidate-model-signals.csv` and quality evidence. Inference does not select or submit reaction candidates; preserve that skill's decision boundary.

Sampling reuses `get_backend("rxnresid").inference_command` with `source_directory` and `route_vocabulary`. Independent workers need the SAR package and dependencies. `tricycle_sar.sampling_runtime.stage(root)` supplies validated package/source/original environment assets and does not execute by default.

## Shared execution contract

Use the `tricycle_sar` environment and runners, without `tools/` compatibility entry points or a separate protocols source tree. Workers consolidate predictions/metrics/status; retain checkpoints explicitly when needed, including every model required by later train/refit/infer stages.

New tasks bind shared environment/entry points through `backends/catalog.json`; common core files are delivered with identical hashes. Maintain only current catalog/source/methods, without historical implementation copies or compatibility variants. Result method IDs record provenance. `results verify` independently recomputes scientific results; `results prepare` builds new inputs with the current backend and neither trains nor submits.

Generic RxnResid train/refit/infer uses request-supplied native configuration, CSV, and checkpoint independently of data origin; reaction-db is an optional adapter. Sampling/five-fold column names, grouping, and route aggregation belong to experiment protocols, not generic model constraints.

`sar.train/infer/finetune` shares command builders, with image/machine matched to the backend. Local source inputs must match the fixed archive. Planning records input hashes and submission rechecks them. `single_job.py` and `sampling_runtime` share initialization/source. A dry-run plan with missing inputs cannot be submitted directly. Cumulative-label training uses from-scratch mode on the full train split; prediction coverage is checked by route identity.
