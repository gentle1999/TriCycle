---
name: sar-benchmark
description: Prepare and publish compact, reproducible grouped five-fold SAR experiments using shared current backends.
---

# SAR Five-Fold Benchmarks

`models` accepts only concrete registered backend names. For Chemprop use `chemprop_cgr` and `chemprop_multicomponent`, listing both for comparison rather than aliases.

## Record contract

Follow [SAR experiment records and archiving](../../../packages/tricycle-sar/README.md#experiment-records-and-archiving). New experiments reuse smoke-validated package backends, not copies of model code, installers, or per-fold manifests. Record shared definitions and actual model/data/fold/seed/round differences without parallel scope/manifest summaries.

The sole implementation is `packages/tricycle-sar/src/tricycle_sar/`. `backends/catalog.json` combines reusable environment/run entries; maintain current initialization/training/inference directly. Dataset and shared split bindings are immutable; dataset size is not a training constraint. Read models/folds from configuration/registry and verify task-table coverage instead of fixing counts in skills.

## Prepare and validate

Copy/fill [example-sar-benchmark.toml](../../../configs/example-sar-benchmark.toml): choose models, provide dataset/existing-split paths and SHA-256, and point `training_protocol.reference` at current backends. Paths resolve against the request file. Templates do not bind local reports or historical data.

Unknown `dataset`/`split` fields are rejected. Existing `split.path` and `split.sha256` must both be valid. Correct missing paths, changed content, or wrong fields; do not delete bindings to regenerate a split. Omitting both is only for a new split under the selected protocol.

```bash
uv run --all-packages python -m tricycle_sar benchmark prepare --config /path/to/filled-benchmark.toml --run-dir /path/to/new-run
uv run --all-packages python -m tricycle_sar benchmark check --run-dir /path/to/new-run
```

These local operations do not connect, read credentials, install, train, or submit. Prepare requires local NumPy and never overwrites nonempty runs. One `run.toml` contains shared definitions and derived-input checks; check creates no duplicate summary. Reuse frozen splits directly rather than rerandomizing published results.

All paths for one substrate group share an outer fold; validation comes only from that fold's training partition, and each path is tested exactly once. Target is `G_T_activate` in kcal/mol; other energies/timings are not features. Recompute metrics from frozen GT. Report five-fold MAE mean/sample SD separately from pooled OOF metrics. Do not present incomplete folds as a complete comparison or select reruns by test error.

## Training jobs

When smoke is needed, CPU/molecular/native preparation uses the respective `tricycle_sar.preparation` module with `--include-smoke`; RxnResid/ReactionT5 uses `tricycle_sar.preparation.smoke`. Forward/backward probes do not establish full training/inference success. Verify probe/environment input hashes in the actual expanded upload directory after `bohr.batch.prepare`, including renamed `__bohr_task_script.sh`.

Use `tricycle_sar.submission` and `tricycle_sar.preparation`: shared fields become `task_defaults`, differences become `task_specs`. Full worker TOML and native snapshot/split/bundle manifests are derived in execution-preparation directories only, not published as results or a second protocol. Default preparation creates formal training requests only. Added smoke requires explicit `--include-smoke`; validated methods are not automatically resmoked.

Callers configure project/group: `requests`, batch, smoke, and environment CLI entry points require `--group-config /path/to/group.toml`; Python uses `group_config=Path(...)`. Configuration includes `project_id`, `job_group_id`; see [example-sar-bohr-group.toml](../../../configs/example-sar-bohr-group.toml). There is no implicit group default. All-model preparation uses `--job-group-name` to bind a new group. Keep model-method bindings separate from scheduling configuration.

Read image/source revision/assets/parameters/initialization/executor from [current backends](../../../packages/tricycle-sar/src/tricycle_sar/backends/catalog.json) and referenced manifests. Do not pin these values in skills or unilaterally reinstall Torch/CUDA, upgrade dependencies, or rebuild environments after node errors. Maintain current shared code directly; routine fixes do not create historical copies/catalog versions. Explicitly reviewed changes update affected bindings and validate them; unexplained hash drift remains an error.

Shared initialization retries transient install/download failures at most three attempts, with 5-second/15-second backoff, 450 seconds per pip call, and shared 1,800-second initialization budget. Reuse pip cache/successful installs. If the default mirror returns no versions, check the official index once; no versions there or other deterministic errors are not retried. GPU initialization checks actual CUDA availability first. Failure is `inspect_error`: inspect Python/Torch paths, host driver, and loaded CUDA libraries from preflight events, rather than diagnosing/replacing nodes from a message such as “driver too old.” Exhausted network retries yield `retry_job`; other failures yield `inspect_error`. Read `status.csv` fields `initialization_retries/failure_reason/next_action` and `worker.log`. A suggested action is not an already-resubmitted job. Training/inference are not rerun unconditionally.

ReactionT5 delivers fixed local weights/tokenizer/config; MolCLR strictly uses fixed pretrained weights; UAM retains its frozen from-scratch method. Stop on missing assets/hash mismatch without HF download fallback. Large assets are externalized by fixed hash.

Authorized execution uses Bohr/internal adapters; prepare/check has no external effects. Query/download existing jobs; failure reruns create new runs while preserving successful/queued/running tasks. Model upgrades start new sampling trajectories without mixed checkpoints. Sampling/full-pool proxy models freeze one route vocabulary from unlabeled prior and retain it through trajectory train/refit/predict.

## Publish results

Result manifests use `backend.sha256` for computational provenance. Missing fields or old hash fields stop verification; do not migrate/rewrite historical results automatically. Provenance hashes need not match current backend hashes.

Experiment result packages, figures, and reproduction reports live in Git-ignored `reports/` or `artifacts/`, outside source commits. General documentation describes current interfaces and acceptance rules only. After all folds complete and validate:

```bash
uv run --all-packages python -m tricycle_sar benchmark publish --run-dir /path/to/completed-run --out-dir /path/to/result-package
```

Export only data, shared splits, one consolidated OOF prediction table, metrics, consolidated method bindings, and protocol references; scientific figures/reproduction notes may be included. All model/fold predictions go in `results/oof-predictions.csv`, retaining model-output labels for rounding checks; recompute fold metrics from that table. Workers consolidate predictions/metrics/status and use one worker.log. Explicitly retain checkpoints needed by later trajectory refit/infer. Merge backend references into one manifest, without separate protocol-ref or per-fold JSON.

Do not publish `results/raw/`, other per-fold prediction copies, recursive run copies, `provenance/`, `inputs/`, job receipts, installation/device/stage logs, or checkpoints. Raw worker predictions remain in `runs/`. Combine applicable folds for one method into one record.

Result packages contain no `scripts/` or `reproduction/`. Use `python -m tricycle_sar results verify|assets|prepare --result-dir /path/to/result-package`, reusing `tricycle_sar.results` and `tricycle_sar.training_inputs`. Offline plots use `python -m analysis.sar render|distributions` with shared root `analysis/` implementation/lockfile; do not copy analysis per experiment or import it from packages. Input preparation uses result model/fold/data/splits with current backends and writes outside the report. Scientific scores can be independently recomputed; exact historical upload-directory reconstruction or bitwise retraining is not promised.

Current `backends/catalog.json` binds shared environment/entry points; common core files retain identical hashes across jobs. Keep only current implementations. Method IDs record provenance; `results verify` recomputes science, while `results prepare` prepares current inputs without training/submission.

## Offline figures

GT activation energy and substrate-group residuals use `python -m analysis.sar distributions --result-dir /path/to/result-package --out-dir /path/to/distributions` in the isolated root analysis environment. Overall/within-group residuals are path-weighted; within-group residual is path value minus `(ene_id, diene_id)` mean. Keep zero residuals for single-path groups and show multi-path groups separately. Group-mean residuals weight groups equally around the mean of all group means. Use population variance ddof=0 and retain all tails. These are not model errors; path-weighted within-group variance is not an equal-group average of variances. Write PNG/PDF/SVG and statistics JSON outside reports, refusing nonempty destinations.

For user-provided native reaction collection CSV requiring only overall/within-group residuals, use `python -m analysis.sar collection-distributions --dataset /path/to/reaction-collection.csv --out-dir /path/to/empty-output --aspect-ratio 4 --width-inches 8`. Read `activation_gibbs_free_energy_kcal_mol`, group by `(ene_id, diene_id)`, and retain all rows/single-path groups without adding multi-path comparisons or other panels. `aspect-ratio` describes the full canvas. This entry point needs no model result package and changes no runtime protocol.

For scientific-figure restyling use the same command with `--style publication`, not another source tree/environment. The main figure is a 183 mm three-panel histogram; ECDF is supplementary. Export SVG/PDF/PNG/TIFF plus caption, alt text, specifications, source hashes, and text/panel layout. Audit text bounds/fonts/alignment before declaring completeness. Do not claim journal compliance without checking its guidance.

Completed jobs whose backend has since changed can generate five-fold MAE boxplots through `python -m analysis.sar boxplot --run-dir /path/to/benchmark --prediction-dir /path/to/compact-predictions --out-dir /path/to/empty-figures`. Use the analysis environment; SAR `collect` independently validates frozen data/splits/complete predictions without replacing historical method bindings. Each dot is one fold's test MAE; require all five and save mean/sample SD separately from pooled OOF metrics.

## Input and run records

`sar.train/infer/finetune` uses shared builders with backend-matched image/machine. Source matches fixed archives, planning hashes inputs, and submission rechecks. `single_job.py`/`sampling_runtime` share initialization/source. Incomplete dry-run inputs cannot be submitted. Cumulative labels use from-scratch full-train mode; predictions validate route identity.

Benchmark workspaces, model preparation, and shared batch inputs use content-addressed project asset storage; reference manifests locate/validate assets without duplicating fixed weights per run. New directories retain `input-assets.json`, not data/weight/input symlinks. `results prepare` returns `run_dir` and `input_assets_manifest`; do not upload the run directly.

Read local assets with `tricycle_core.asset_store.resolve_input`. Internal execution calls `tricycle_sar.delivery.materialize` for disposable inputs and cleans up; Bohr adapters materialize/upload/clean automatically. Diagnostic logs may remain in run results. `retain_checkpoint` selects final model paths only; uploaded inputs, preprocessed tables, and intermediate weights are not returned. Formal packages retain the compact contract above.

Preserve original content/protocol/split hashes; storage migration must not regenerate scientific bindings. Historical runs are neither automatically migrated nor deleted.

## Publishing after storage-only implementation changes

Use `results publish-recorded --run-record ... --dataset ... --splits ...
--predictions ... --out-dir ...` when publishing completed evidence whose original
execution binding differs from the current checkout. Verify extracted archive
members first. This path preserves recorded methods and recomputes science without
loading historical executables; it does not relax frozen live execution checks.
Compare scoped scientific identities separately from full delivery hashes. Unknown
or mixed code remains scientific unless an explicit reviewed selector identifies
a post-result storage boundary. Retain dataset/split/seed/environment bindings and
report missing historical evidence rather than synthesizing it. Follow
[reproducible result packages](../../../docs/reproducible-results.md).
