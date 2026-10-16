---
name: sar-model
description: Prepare unified SAR model benchmarks and validated backend tasks, including RxnResid train/refit/infer, with compact shared records.
---

# SAR Model Tasks

`tricycle-sar` owns shared model backends, environments, training/evaluation protocols, preparation, and result archiving. Select models and prepare benchmarks with `get_backend(model)`, `tricycle_sar.preparation`, and `python -m tricycle_sar`. Methods come from package `backends/catalog.json`. Frozen methods mean smoke-validated initialization/training/evaluation scripts reused directly from SAR, without new environments or dependency upgrades.

Batch workers use `backends/workers/unified.sh` and registry-bound Python runners. Do not create model-specific/environment-specific shell forwarders or `tools/` wrappers. Backend preparation directly stages shared execution/collection scripts. Initialization runs `backends/environments/engine.py`, delivered by catalog as `common/sar_model_envs/engine.py`; sampling/local environments share this engine.

RxnResid asset configuration requires current `schema` and `[model]`; see package README for fields. Unknown top-level/table fields are errors. `tricycle_sar.SarPlugin` and `sar.train`, `sar.finetune`, `sar.infer` expose RxnResid tasks. `assets/rxnresid-model.toml` binds source/entry points: `train.py`, `train.py --refit --checkpoint ...`, and `predict.py`. Other models predict through their training/evaluation runners; plan independent checkpoint inference/refit only when the backend supports it.

## Workflow and records

Follow [SAR experiment records and archiving](../../../packages/tricycle-sar/README.md#experiment-records-and-archiving). Shared data/splits/methods/parameters are defined once; tasks record fold/seed/round differences. `tricycle_sar.records.compact_tasks` and `tricycle_sar.submission` normalize them. Do not copy per-fold JSON, installers, or parallel manifest/scope summaries.

1. Specify data, checkpoint, and actual command through TOML/artifacts, with matching source versions/hashes.
2. Prepare multi-model jobs through backend/benchmark interfaces and native RxnResid through `sar.train/finetune/infer`. Default execution has no network/submission.
3. Within existing authorization use the bound Bohr/internal executor. RxnResid submission uses injected Bohr adapter with explicit `--live`; existing job IDs go to status/download.
4. Keep logs, checkpoints, receipts, and recovery state in `runs/`. Export formal scientific results through module interfaces, with one consolidated OOF table; exclude per-fold prediction copies, `provenance/`, raw worker inputs, and per-task logs.

Refit restores model/config/fold/split without overrides. Source upgrades require new training/sampling trajectories, without mixed checkpoints. Native aleatoric/epistemic/predictive uncertainty goes to sampling only after coverage and fixed-route-vocabulary validation. Detailed workflows: [sar-train](../sar-train/SKILL.md), [sar-finetune](../sar-finetune/SKILL.md), [sar-infer](../sar-infer/SKILL.md), [sar-benchmark](../sar-benchmark/SKILL.md).

## Shared execution

Prefer `get_backend`, `tricycle_sar.preparation`, and `python -m tricycle_sar`; do not maintain external model protocol implementations. Default worker outputs consolidate predictions/metrics/status. Retain checkpoints explicitly for later refit/inference.

Sampling reuses backend `training_command`, `refit_command`, and `inference_command`; data/checkpoint updates use `tricycle_sar.rxnresid`. Stage validated source archives and original environment entry points with `tricycle_sar.sampling_runtime.stage`, then `verify` before execution. Do not fall back to historical readiness versions.

Training, sampling, and demos share current backends. `python -m tricycle_sar.preparation.environment --group-config /path/to/group.toml` explicitly binds project/group and generates uploadable inputs. `results prepare` reuses existing data with current methods in a new directory; verification independently recomputes recorded scientific results. Core owns shared metrics/I/O/hashing; `tricycle_sar.predictions` owns prediction validation and substrate-pair aggregation.

Generic RxnResid train/refit/infer accepts native configuration/CSV/checkpoints independently of data source; reaction-db is optional. Sampling/five-fold columns, grouping, and aggregation remain experiment-specific.

Offline statistics/figures use shared root [`analysis/`](../../../analysis/README.md) implementation/environment through `python -m analysis.sar render|distributions`. `tricycle_sar results` retains verify/prepare/assets; packages never import analysis code.

Shared command builders match image/machine to backend. Source inputs match the fixed archive; planning hashes inputs and submission verifies again. `single_job.py` and `sampling_runtime` share initialization/source. Incomplete dry-run inputs cannot be submitted. Cumulative-label training uses from-scratch mode on full train splits; predictions validate route identities.

## Input and run records

Prepared directories use content-addressed `input-assets.json` references, without input directory views, data/weight copies, or execution-input symlinks. Do not manually alter shared weights. `results prepare` returns `run_dir` and `input_assets_manifest`; the run directory is not directly uploadable.

Read local assets through `tricycle_core.asset_store.resolve_input`. Internal/local executors use `tricycle_sar.delivery.materialize(prepared, disposable)` to validate and materialize isolated temporary inputs, then clean up. Bohr adapters materialize/upload/clean automatically. Do not upload symlinks or train within shared views.

Workers collect model-final-output allowlists. `retain_checkpoint` preserves newly generated final models only, excluding uploaded weights, raw/preprocessed data, and intermediate checkpoints. Native single-job relative `--output` must be separate from input/source directories; download only declared result paths and worker logs.

Preserve content/protocol/split hashes; storage migration does not justify regenerating scientific bindings. Existing runs are not automatically migrated or deleted.
