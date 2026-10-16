# Offline analysis and plotting

`analysis/` contains offline result analysis and scientific plotting, sharing this directory's `pyproject.toml` and `uv.lock`. Model tasks, sampling decisions, environment setup, and scientific result validation belong to `packages/`. Analysis consumes those interfaces and experiment artifacts; packages must not depend on `analysis`.

New analysis outputs belong in `reports/<topic>/<report-id>/`, outside source runs; see [run record scope](../docs/run-records.md). This includes frozen comparison snapshots, tables, figures, and comparison-observer status/logs. Read original run evidence in place and reference its paths/hashes instead of copying entire runs. Reaction-collection distribution defaults to `reports/<date>/<timestamp>/reaction-collection-distribution/`; explicit output directories remain supported.

| Location | Contents |
| --- | --- |
| `report_figures.py` | Consistent academic layouts for published five-fold, sampling, and language-policy report figures |
| `sar/figures.py` | Five-fold MAE, OOF parity, absolute-error CDF, and GT distribution plots |
| `sar/distributions.py` | Descriptive statistics for overall GT, within-group residuals, and group-mean residuals |
| `sar/distribution_figures.py` | Publication layouts, captions, and provenance for GT distributions |
| `sampling/replay_sampling_panels.py` | Frozen snapshots and plots for current literature baselines and intelligent sampling |
| `sampling/language_comparison.py` | Completion observer and paired budget comparisons for English and historical Chinese sampling policies |
| `sampling/threshold_cost.py` | Cost to first reach specified GT MAE, paired comparisons, and threshold coverage |
| `sampling/publication_curves.py` | Three-space sampling curves, empirical full-pool error references, and threshold costs from verified snapshots |
| `sampling/route_audit.py` | Checks executed intelligent decisions, replays rankings, and summarizes stage-specific structure, cost, and route changes |
| `sampling/rxnresid_active_learning_analysis.py` | Cost correction, merging, and plotting of downloaded sampling trajectories |
| `sampling/reaction_collection_distribution.py` | Offline distributions of reaction collections, route coverage, and costs |

Use the specific analysis modules below; no `tools/` forwarding entry points are maintained. `tricycle-sampling` owns structural features, validation feedback, cost metrics, and runtime summaries for online exploration. `tricycle-reaction-db` owns database substrate identity adaptation.

## Usage

To render a consistent report figure set from a combined published result package:

```bash
uv run --project analysis --locked python -m analysis.report_figures \
  --report-dir /path/to/combined-result-package --out-dir /path/to/empty-figures
```

This presentation-only renderer checks the selected table hashes against the package inventory,
preserves published statistics and coverage, and exports PNG/PDF/SVG with a source/output manifest,
captions, and alt text. Outputs must be outside the publication; nonempty directories are rejected.
Verify the scientific packages with their owning runtime verifiers before rendering.

Run from the repository root. Analysis entry points read local workspace package interfaces directly; the isolated environment contains analysis dependencies only:

```bash
uv run --project analysis --locked python -m analysis.sar render \
  --result-dir /path/to/result-package --out-dir /path/to/empty-figures
uv run --project analysis --locked python -m analysis.sar distributions \
  --result-dir /path/to/result-package --out-dir /path/to/empty-distributions
uv run --project analysis --locked python -m analysis.sar distributions \
  --result-dir /path/to/result-package --out-dir /path/to/empty-publication --style publication
```

SAR analysis first calls `tricycle_sar.results.verify` to validate scientific results. Outputs must be outside the result package; nonempty output directories are rejected. Five-fold figures export PNG/PDF/SVG. Publication distribution figures export SVG/PDF/PNG/TIFF and record hashes of the actual source and dependency lock. Figure values use the same frozen tables and statistical definitions.

For a two-panel distribution figure read directly from a native reaction-collection CSV:

```bash
uv run --project analysis --locked python -m analysis.sar collection-distributions \
  --dataset /path/to/reaction-collection.csv --out-dir /path/to/empty-figures \
  --aspect-ratio 4 --width-inches 8
```

Inputs must include `activation_gibbs_free_energy_kcal_mol`, `ene_id`, and `diene_id`. Panel a shows overall activation free energy; panel b shows each route value minus its `(ene_id, diene_id)` group mean. All CSV rows receive equal route weight. Single-route groups contribute zero residual; no curve excludes them. Aspect ratio describes the entire canvas, without tight cropping. Outputs include SVG/PDF/PNG/TIFF, statistics, captions, and provenance. This entry point validates CSV fields and finite values without requiring a five-fold model result package.

For the current sampling suite, export a snapshot before rendering in the analysis environment:

```bash
uv run --project analysis --locked python -m analysis.sampling.replay_sampling_panels --help
uv run --project analysis --locked python -m analysis.sampling.reaction_collection_distribution --help
uv run --project analysis --locked python -m analysis.sampling.threshold_cost \
  --run-dir /path/to/sampling-root --out-dir /path/to/new-threshold-report
```

Threshold-cost reports read verified snapshots and take cumulative core-hours at the first checkpoint reaching MAE ≤ 5, 4, 3, 2, or 1. They do not interpolate or substitute later best errors. Means, sample standard deviations, and cost-saving rates are reported only when all five seeds reach the threshold. Incomplete cells show the number of seeds reaching it; missing successes are neither zero nor excluded to average only successful seeds. First attainment does not imply sustained attainment. Core-hours include initial sampling and exclude SAR training and agent overhead. Per-seed records are exported alongside figures.

Three-space publication curves consume verified snapshots from `replay_sampling_panels`:

```bash
uv run --project analysis --locked python -m analysis.sampling.publication_curves \
  --snapshot /path/to/snapshot.json --pool-pairs 1600 --out-dir /path/to/new-curves
```

Set `--pool-pairs` to the number of substrate pairs in the experiment's full sampling pool. Dashed references show the mean final MAE of the five LHS seeds after exhausting the pool; they are empirical references, not pure computational error or theoretical lower bounds. Curves use means and sample SDs of the latest observed checkpoint at each shared cost, without extrapolation. Solid segments include five seeds; dotted segments have fewer seeds. Both combined and individual three-space figures use a full-canvas ratio of 4:1.5. Exports include editable SVG, PDF, 600 dpi PNG/TIFF, curve data, per-seed first-attainment costs, method references and adaptation notes, captions, and snapshot hashes.

Audit intelligent sampling routes with `python -m analysis.sampling.route_audit --snapshot /path/to/snapshot.json --out-dir /path/to/new-audit`. Include only decisions whose next-round outcomes are complete in the snapshot. Verify accepted responses, input hashes, selected IDs, and the next round's actual revealed results; replay both the current and previous formulas on the current pool. Initialization and controller-driven selection of all remaining candidates are not agent decisions. Stage statistics are averaged within each seed first, then equally across available seeds, retaining round and seed counts per stage. Late-stage statistics from partial trajectories must not be presented as complete five-seed results.

For a matched language-policy comparison, run `python -m analysis.sampling.language_comparison --plan /path/to/comparison-plan.json --out-dir reports/language-comparison/<report-id> --watch`. The `tricycle.language-comparison/v1` plan lists `experiments` with `project`, English `root` and `suite`, `historical_experiment`, and the `english_experiment_sha256` and `historical_experiment_sha256` bindings. Budgets come from the bound experiment configurations and must match. Keep new comparison plans and output directories under `reports/`, outside the experiments. The observer freezes historical evidence immediately, then waits for every English seed to become complete or blocked; without `--watch`, it checks once. It never submits, retries, or changes experiments. An external service may keep the observer running; `--poll-seconds` defaults to 60. Preserve explicitly configured historical report paths during recovery.

Outputs include immutable cohort snapshots, `status.json`, `paired-seeds.csv`, `budget-summary.csv`, `comparison.json`, and `REPORT.md`. Pair the same seed at each original budget using its latest affordable checkpoint within observed coverage, without extrapolation. Negative English-minus-Chinese GT MAE favors English. Partial cells show paired counts, and SD is omitted for one pair. A hash mismatch stops reporting rather than accepting changed evidence. Report language, narrative-limit, infrastructure, scheduling, and model-service differences explicitly; this comparison alone does not establish a causal language effect.

Reaction-collection analysis requires explicit `--ground-truth`, `--expected-pairs`, and at least one `--project NAME=EXPORT_DIR`. Counts come from inputs, not historical run defaults.

Result verification, training-input preparation, and fixed-asset indexing still use `python -m tricycle_sar results verify|prepare|assets`. Result packages contain scientific results and figures and reuse this directory's analysis implementation and environment. Experiment reports and figures belong in Git-ignored `reports/` or `artifacts/`, outside source commits.

For completed benchmarks, produce a fold-MAE boxplot with one point per fold:

```bash
uv run --project analysis --locked python -m analysis.sar boxplot \
  --run-dir /path/to/benchmark --prediction-dir /path/to/collected-predictions \
  --out-dir /path/to/empty-boxplot --xscale log --aspect-ratio 2 --width-inches 7.2
```

This entry point calls SAR `collect`, verifies frozen inputs, splits, complete OOF identities and targets, and recomputes metrics independently of changes to current execution backends. It preserves the run's original method bindings for audit. Predictions may use `predictions.csv` or the existing compact worker layout and must cover all five folds for every model.

Boxes show the interquartile range, center lines the median, and whiskers the extreme observations within 1.5 IQR; every fold is plotted as a point. Defaults match GT distribution figures: blue-purple fill, Arial, thin dark axes, no grid, and short titles. The MAE axis is logarithmic and the full canvas is 2:1, without tight cropping. Full statistical definitions are saved to `figure-caption.txt`. Each box is annotated to the right of its rightmost fold point with the mean ± sample SD (ddof=1) of the five raw MAEs; exact values are exported separately.

A logarithmic axis changes display only; boxes, means, and sample SDs are calculated from raw MAE. Zero values must not be shifted or deleted; explicitly choose `--xscale linear` instead. Exports also include per-fold metrics, input hashes, PNG/PDF/SVG/TIFF, 600 dpi rasters, and editable vector text. This analysis does not change submitted training protocols.

Portable replay result packages can be rebuilt with
`python -m analysis.sampling.published_report --result-dir /path/to/package --out-dir /path/to/new-analysis`.
This verifies all path-level predictions and acquisition costs before producing
paired English/Chinese curves and budget tables. See
[reproducible result packages](../docs/reproducible-results.md) for export,
verification, target precision, and scientific-versus-delivery identity semantics.
