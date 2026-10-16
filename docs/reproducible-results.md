# Reproducible scientific result packages

Publish scientific observations separately from runtime records. A report must
support recomputing its numbers, not merely display exported figures. Keep raw
execution evidence in its original run/archive and record source paths and hashes.
Do not copy virtual environments, checkpoints, installation logs, or complete runs
into a report.

## Completed five-fold benchmarks

`tricycle_sar.results.publish_recorded` publishes a completed benchmark from its
original `run.toml`, frozen dataset/splits, and consolidated worker predictions:

```sh
uv run --all-packages python -m tricycle_sar results publish-recorded \
  --run-record /path/to/original/run.toml \
  --dataset /path/to/dataset.csv --splits /path/to/splits.csv \
  --predictions /path/to/predictions.csv --out-dir /path/to/new-result-package
uv run --all-packages python -m tricycle_sar results verify \
  --result-dir /path/to/result-package
```

The publisher verifies frozen input hashes, group separation, complete OOF
coverage, identity, targets, and finite predictions; it recomputes fold and pooled
metrics. Original method IDs and backend hashes remain provenance. It does not
load an old executable registry or replace historical bindings with today's ones.
Current-backend publication retains its strict executable-source validation.

## Replay observations and language comparisons

`tricycle_sampling.replay.published export` reads an explicit JSON plan whose
`cohorts` entries have `name` (`chinese` or `english`), `root` (readable original or
selectively restored sampling root), and `original_root` (its immutable original
path). Each root contains the original sampling plan, suite/experiment bindings,
seed states, completed evaluation summaries, and result asset references. Preserve
original bytes when restoring archives; verify extracted members against the
archival inventory. Shared assets must remain readable during export.

```sh
uv run --all-packages python -m tricycle_sampling.replay.published export \
  --plan /path/to/export-plan.json --result-dir /path/to/new-sampling-package
uv run --all-packages python -m tricycle_sampling.replay.published verify \
  --result-dir /path/to/sampling-package
uv run --project analysis --locked python -m analysis.sampling.published_report \
  --result-dir /path/to/sampling-package --out-dir /path/to/new-analysis
```

The package keeps one GT table, per-space pools/labels/prior inputs, original
experiment configurations, a consolidated table of checkpoints, incremental
acquisitions, and a losslessly compressed float64 prediction matrix. Matrix rows
follow `checkpoints.csv`; columns follow `evaluation.csv`. Accepted decisions are
consolidated as compressed JSONL, preserving historical language. Source hashes
refer back to immutable evidence. The verifier needs only package files and the
current validation code, not original runs, asset stores, model checkpoints, or
network access. It recomputes every MAE/RMSE/bias and sampling cost, checks initial
selection pairing, and rejects incomplete trajectories labeled complete.

Replay workers historically computed metrics using their exported target column,
which may be float32-rounded GT. The package preserves that representation for
exact reproduction and separately measures its difference from metrics computed
against unrounded GT. It does not rewrite historical scores or silently change
precision. Five-fold publication continues to use its existing frozen-GT metric
contract.

The analysis command rebuilds paired budget tables and language comparison curves.
Use identical paired seeds, the latest affordable checkpoint within both observed
domains, and explicit coverage. Never extrapolate blocked seeds, select a favorable
checkpoint, or attribute a policy comparison with execution repairs solely to
language. Add reviewed repair records in `protocols/interventions.json` before
finalizing package checksums. Archive/checkpoint hashes establish provenance;
scientific verification does not claim bitwise retraining or GPU determinism.

## Scoped method identity

A catalog or complete delivery hash is an integrity binding, not a sufficient
scientific compatibility test. New benchmark records include `method_identities`:

- `scientific_sha256` binds per-model scientific source components, parameters,
  fixed assets, and numerical environment settings (including image and machine).
- `delivery_sha256` retains the complete existing file fingerprint and method ID.
- Component digests and the selector hash explain the partition and make its
  interpretation auditable. Dataset, split, target, and seed bindings remain in
  the experiment record and must also match for a comparison.

`tricycle_sar.protocol_identity.compare` reports scientific and delivery equality
separately. It refuses comparisons across different identity selectors. Unknown
files are scientific dependencies by default. The only mixed-file selectors
currently exclude the known post-result output hook from `sar_worker_common.sh`
and artifact-retention tails/helpers from `sar_outputs.py`. Environment setup,
prediction collection, and metric calculation remain bound. Unexpected selector
boundaries require review. This is an explicit reviewed partition, not automatic
semantic equivalence detection for arbitrary edits.

Storage changes can alter delivery identity without changing scientific identity.
They still require result-integrity tests. Frozen live submissions continue to
verify exact delivered bytes; a matching scientific hash does not authorize
silently replacing execution files or rewriting historical hashes. Historical
source comparisons may reconstruct component identities from archived bytes;
do not invent missing historical environment metadata.
