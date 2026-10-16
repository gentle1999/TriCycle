# SAR model benchmarks

Multi-model benchmarks reuse the [SAR package](../packages/tricycle-sar/README.md)'s current backends. The [catalog](../packages/tricycle-sar/src/tricycle_sar/backends/catalog.json) defines models, environments, and executors; [benchmark configuration](../configs/example-sar-benchmark.toml) binds data and experiments. Follow the [sar-benchmark skill](../.agents/skills/sar-benchmark/SKILL.md).

## Shared experimental conditions

All routes for a substrate group stay in the same outer fold. Validation data comes only from that outer fold's training partition. All models share data and splits; complete OOF predictions must cover every route exactly once. Recompute metrics against the bound ground-truth labels. Report fold MAE means/sample standard deviations separately from pooled OOF metrics. Never choose rerun results by test error.

## Preparation, execution, and verification

Run local `benchmark prepare/check`, then generate tasks with `requests --group-config`. By default, prepare formal training requests only; smoke runs require explicit opt-in. Accept model forward/backward probes separately from post-training prediction. Select Bohr/internal executors through the catalog. Existing job IDs proceed to status checks and downloads; failed reruns create new runs.

After all planned tasks complete, `benchmark publish` exports data, common splits, merged OOF predictions, metrics, method bindings, and a manifest. `results verify` independently recomputes scientific results. See [SAR experiment records and archiving](../packages/tricycle-sar/README.md#experiment-records-and-archiving) for full commands and output contracts.

Experiment reports, score tables, figures, and reproduction records belong in Git-ignored `reports/` or `artifacts/`, outside this guide and source commits. Original receipts and logs remain in `runs/`. Offline plotting uses [analysis/](../analysis/README.md); result packages do not copy execution or plotting source.
