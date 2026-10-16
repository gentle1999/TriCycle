---
name: sampling-strategy
description: "Select the next batch of reaction candidates with a registered, auditable sampling strategy."
---

# Sampling Strategies and Protocols

Use when candidate pool, history, and current predictions are ready for the next selection. This skill defines selection boundaries; see the [matched-experiment guide](../../../docs/mae-intelligent-sampling.md) for multi-round execution/recovery and [candidate-select](../candidate-select/SKILL.md) for one TOML capability.

## Prepare data and choose comparisons

For database refreshes, use [export and substrate adaptation](../reaction-db-export-rxnresid/SKILL.md) under the [adaptation contract](../../../docs/reaction-dataset-adaptation.md), freezing data hashes, identities, and matching strategy. Retain the complete unsampled pool, unique IDs, current predictions, and revealed valid-label history.

Prior structures/route counts may be available in advance. Future labels, calculation success, actual timings, GT metrics, and unsampled residuals cannot guide acquisition. Proxy/GT structure overlap alone is not label leakage; distinguish label origin and permitted use.

The current `LITERATURE_BASELINE_STRATEGIES` profile contains predictive-uncertainty, latin-hypercube, ucb, distance-uncertainty-product, and uncertainty-kriging-believer. Each has a distinct primary citation in `packages/tricycle-sampling/src/tricycle_sampling/protocols.py`; see [methods and evidence](../../../docs/sampling-baselines.md). Random/epistemic/aleatoric are separate strategies outside the main comparison.

New matched experiments use the [six-method suite](../../../configs/dpa-unified-sampling-suite.toml): five literature baselines plus intelligent sampling, sharing pool, initial set, main training, evaluation, seeds, batch size, and core-hour accounting. Intelligent starts with a coverage/cost proposal, adapts using revealed feedback, and receives one batch review. Follow [agent-authored-acquisition](../agent-authored-acquisition/SKILL.md) for expressions. Do not attribute engineering coverage heuristics/custom cost formulas directly to the five papers.

## Workflow

1. Validate pool completeness, IDs, provenance, budget, and seed.
2. Plan through `SamplingRequest` and `SamplingPlugin.plan("select", ...)`.
3. Call `select`; preserve complete selected/remaining, parameters, provenance, scores or sequential batch steps.
4. Downstream computation consumes accepted selected IDs with mappings to actual inputs; selection itself submits nothing.
5. Update through shared training after labels return, then prepare the next round without redoing accepted selections.

Shared modeling calls `tricycle_sar.get_backend("rxnresid")` train/refit/infer; `tricycle_sar.rxnresid` owns round data/checkpoint updates. New workers use `tricycle_sar.sampling_runtime.stage` with [current backends](../../../packages/tricycle-sar/src/tricycle_sar/backends/catalog.json), bind `sar_backend`, and validate before execution/recovery. Versions/hashes come from configuration/manifests, not skills.

Matched baselines use `experiment_sampling_request()` / `select_experiment_candidates()`, schema `tricycle.matched-sampling/v1`. Shared inputs are six-dimensional `count/(1+count)` descriptors, prior-route mean and summed predictive/epistemic variances, and revealed valid-label references. See [package interfaces](../../../packages/tricycle-sampling/README.md).

## Strategy constraints

- Predictive ranks total variance descending. Generic uncertainty reads dimensionally explicit `*_variance`, then `*_std`, then `*_uncertainty`, or derives classification entropy from `posterior_probabilities`. Use canonical fields; old spellings/nested score tables are rejected. Regression adaptation is not a complete reproduction of classification entropy.
- LHS freezes seed, dense representation, bounds, and design_size. Across rounds only advance offset and remove selected candidates. Discrete mapping/batch prefixes do not guarantee strict Latin stratification.
- UCB uses Gaussian reward posterior quantiles. The shared adapter converts RxnResid epistemic variance to route-mean variance `sum(v_epi)/n²`, with quantile 0.95 and minimize direction. Total predictive variance is not substituted.
- A Score is nearest labeled-reference Euclidean distance × specified variance. Freeze the reference set within the batch; changing to a weighted sum requires a new version.
- KB updates its session with predicted temporary labels before scoring the next item. The shared adapter uses RxnResid marginals plus structural RBF correlation with length scale 1; this is not a native RxnResid joint posterior. No static top-k degeneration or temporary labels in formal training.

Independent API settings live in `metadata.baseline`; [TOML examples](../../../configs/sampling-baselines) demonstrate interfaces, not complete matched experiments. KB may inject a local factory; record adapter ID/version, seed, failures, and overhead. Planning validates only, without session creation/refit.

## Records and recovery

All five strategies break ties by ascending ID. The plugin generates `sampling_protocol`/`sampling_details`; the matched adapter generates `sampling_experiment`. Preserve protocol/implementation version, DOI, rule, adaptation, shared-input hash, and complete decisions. Identical snapshots with different strategies should share input hashes; diverged histories need not.

Current random canonicalizes order before local sampling; first-n retains input order. Preserve historical selections without loading old implementations to rebuild them. Requests/manifests use registered canonical IDs; old aliases are errors.

Stop and repair missing predictions, invalid parameters, or hash mismatch. Do not fall back to random, inspect future truth, or refresh hashes to pass. Recovery preserves selections, model chains, and receipts. New sampling rules do not authorize SAR protocol/image/Torch/CUDA or historical-definition changes.

Round assets share project `asset_store`; RxnResid staging contains only the selected backend. Record per-round input references/hashes; Bohr adapters assemble complete temporary delivery. Follow [campaign shared assets](../exploration-campaign/SKILL.md#shared-assets-and-compact-inputs). Compact storage does not change training, prior routes, evaluation isolation, or checkpoint inheritance.
