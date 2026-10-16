---
name: candidate-select
description: "Select the next reaction candidates deterministically from a TOML sampling request."
---

# Select Reaction Candidates

Select a batch from an explicit candidate pool and preserve strategy, provenance, parameters, and seed. This capability does not train models, query databases, or submit calculations; separate adapters prepare external inputs.

For multi-round historical replay see the [matched-experiment guide](../../../docs/mae-intelligent-sampling.md); for agent-written round formulas use [agent-authored-acquisition](../agent-authored-acquisition/SKILL.md).

## Inputs

TOML contains `strategy`, nonnegative `budget` no larger than the pool, `seed`, `candidates` array of tables, and optional `metadata`. Each candidate has unique nonempty `candidate_id` and `payload`. The generic strategy defaults to random; literature comparisons must choose explicitly.

| Canonical strategy | Primary DOI |
| --- | --- |
| `predictive-uncertainty` | `10.1038/s41467-025-59812-0` |
| `latin-hypercube` | `10.1126/science.adj1817` |
| `ucb` | `10.1038/s41586-024-07021-y` |
| `distance-uncertainty-product` | `10.1038/s41467-024-49011-8` |
| `uncertainty-kriging-believer` | `10.1021/jacs.5c02218` |

Sampling `protocols.py` is authoritative; see the [literature inventory](../../../docs/sampling-baselines.md). `random`, `epistemic-uncertainty`, and `aleatoric-uncertainty` are separate strategies outside this comparison set.

LHS/UCB/A Score/KB settings live in `metadata.baseline`, with unknown fields rejected. LHS needs dense representations, fixed bounds, and integer seed; UCB needs reward posterior mean/std, quantile, and direction; A Score needs labeled reference structures; KB needs a joint posterior or explicitly injected local surrogate factory. See [package interfaces](../../../packages/tricycle-sampling/README.md) and [five TOML examples](../../../configs/sampling-baselines/).

## Execution

1. Validate the complete unsampled pool, unique IDs, field provenance, finite values, and budget. Preserve exported identities/frozen hashes.
2. Validate/save a plan with `SamplingPlugin.plan("select", ...)`. KB planning creates no session and performs no refit.
3. Select and preserve selected/remaining, seed, protocol, effective parameters, and decision details.
4. Hand selected IDs to an explicit downstream computation workflow. Do not write results into the original request or overwrite accepted selections.

```bash
uv run --package tricycle-agent tricycle-agent run \
  candidate.select configs/sampling-baselines/ucb.toml --run-id sampling-ucb-demo
```

Results generate `metadata.sampling_protocol` and `metadata.sampling_details`; requests cannot forge these result fields. All five strategies break ties by ascending candidate ID. Preserve complete inputs/source version, not only strategy name.

## Matched experiments

Use `experiment_sampling_request()` / `select_experiment_candidates()` with `tricycle.matched-sampling/v1`. The five strategies share fixed six-dimensional structural representations, prior routes, RxnResid predictions, and revealed valid-label references; only selection changes. Both baseline controllers use the same adapter. Shared-input hashes bind the current round snapshot.

Bayes-UCB approximates route-mean energy as Gaussian using epistemic variance `sum(v_epi)/n²` with minimize direction; predictive variance is not silently substituted. KB adds fixed structural RBF correlation and conditions sequentially within batches, without retraining main SAR or adding temporary labels to formal training. LHS freezes total design size/bounds and advances offset each round rather than resetting bounds from the remaining pool.

Candidate truth, unsampled success masks, future actual costs, and GT metrics are excluded. Complete missing shared predictions or stop; do not fall back silently to random. Strategy adaptation does not authorize changing frozen SAR training or refreshing hashes to continue historical experiments.

## Failure recovery

Correct invalid candidates/budget/strategy/posterior and create a new local selection run. Sampling has no remote submission/recovery stage. Resume existing trajectories only after checking current implementation/input bindings against run records, preserving accepted selections and remote receipts without historical controller source copies.
