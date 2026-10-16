# Sampling replay entry points

TriCycle builds new active-learning trajectories from historical calculation results: select substrate pairs, reveal available proxy labels, update that trajectory's RxnResid model, then evaluate against frozen ground truth. Selection sees only prior structures, current predictions, and revealed history. Future labels, calculation outcomes, actual core-hours, and GT metrics remain private to replay and evaluation.

The comparison includes five literature baselines and the `intelligent` strategy. Intelligent sampling combines structural coverage, cost, model feedback, and batch review. All methods share candidate pools, matched initial sets, main-model training, fixed evaluation, and budget definitions. Random selection is used only for shared initialization, not as a new comparison curve.

## Choosing an entry point

| Need | Entry point | Configuration or documentation |
| --- | --- | --- |
| Prepare and advance round-by-round method comparisons | `tricycle_sampling.replay.suite`, reusing `tricycle_sampling.replay.campaign` | [Unified suite configuration](../configs/dpa-unified-sampling-suite.toml), [experiment and recovery guide](mae-intelligent-sampling.md) |
| Run a complete baseline trajectory per remote worker | `tricycle_sampling.active_learning` | [Five-baseline configuration](../configs/rxnresid-matched-sampling.toml) |
| Select one local candidate batch | `candidate.select` / `SamplingPlugin` | [Sampling package interface](../packages/tricycle-sampling/README.md), [standalone TOML examples](../configs/sampling-baselines/) |
| Inspect method sources and evidence of use | Versioned literature protocols and reference registry | [Methods and references](sampling-baselines.md) |
| Verify a fixed candidate space | Frozen data and substrate identity adaptation | [Sampling spaces](frozen-sampling-space.md), [dataset adaptation](reaction-dataset-adaptation.md) |

Both baseline execution entry points call `select_experiment_candidates()` with protocol `tricycle.matched-sampling/v1`. The round-based suite runs intelligent sampling through `intelligent`. Sampling and core experimental definitions are shared; scheduling granularity and artifact layouts remain distinct. Interface consistency alone does not justify merging results.

## Preparing a new experiment

```bash
uv run --package tricycle-agent python -m tricycle_sampling.replay.suite prepare \
  --config configs/dpa-unified-sampling-suite.toml
```

Preparation creates only local suite records, configuration, frozen bindings, and initial worker inputs. Configuration references existing local data and prior structures; verify actual paths and hashes. Missing inputs are neither downloaded nor replaced automatically. Remote execution requires existing experiment authorization and explicit live/submit flags; see the [operating guide](mae-intelligent-sampling.md).

Every round follows the shared sequence: reveal labels → train/infer within the trajectory → fixed evaluation → prepare structures and acquisition inputs → select candidates → next round. The five baselines use fixed rules. Intelligent sampling authors expressions from coverage, cost, and feedback, then reviews the actual batch. Bayes-UCB uses a Gaussian approximation from RxnResid epistemic variance; KB adds an explicitly recorded structural RBF correlation assumption and within-batch conditioning.

## Run records and archives

Suites must explicitly configure `sampling_strategies`. See the sampling package for random and uncertainty methods in the general candidate-selection API. Preserve existing experiment records; current code does not migrate configurations automatically. Strict run checks reject continuation when source, configuration, data, or worker hashes differ. Start new experiments with current SAR backends rather than refreshing hashes in existing runs.

Run status, machine paths, submission receipts, logs, and temporary recovery records belong in `runs/`. Formal results become independent result packages under Git-ignored `reports/` or `artifacts/`, outside source commits. This guide and its related documents maintain reusable protocols; an individual run's actual counts or completion status are not universal experimental constraints.
