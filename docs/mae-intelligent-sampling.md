# Unified sampling experiments: baselines, intelligent sampling, and execution

New experiments share candidate pools, matched initial sets, the main RxnResid model, training and checkpoint updates, evaluation, and budget accounting. Only the selection strategy varies. The main configuration, [dpa-unified-sampling-suite.toml](../configs/dpa-unified-sampling-suite.toml), includes five literature baselines and intelligent sampling (`intelligent`). Read the suite identifier from configuration. This guide maintains protocols and procedures; each run records its actual counts, status, and recovery evidence.

See [baseline references](sampling-baselines.md) for sources and the [sampling package](../packages/tricycle-sampling/README.md) for requests, variance formulas, and adapters.

## Methods and shared conditions

| Method | Selection rule or distinguishing behavior |
| --- | --- |
| `predictive-uncertainty` | Descending sum of total predictive variance across routes |
| `latin-hypercube` | Frozen LHS sequence mapped to candidates in the shared structural space |
| `ucb` | RxnResid Gaussian Bayes-UCB with fixed 0.95 quantile, favoring lower predicted energy |
| `distance-uncertainty-product` | Nearest distance to the revealed-label set × sum of total route variance |
| `uncertainty-kriging-believer` | Sequential selection by epistemic standard deviation, with within-batch structural RBF posterior conditioning |
| `intelligent` | Adaptive rules based on structural coverage, model feedback, and estimated cost, followed by one batch review |

The intelligent strategy's reference, feedback, and review are part of one strategy definition. All methods use the same main model and evaluation; the five baselines do not call a decision model. The general candidate API additionally supports random, epistemic, and aleatoric selection; this suite plots only its explicit method list.

The round-based `replay.suite` interface requires five matched initial sets, 40 evaluation rounds per trajectory, batches of 40 pairs, and the shared 300-epoch recipe. These are current controller constraints, not completion statistics from one run. Each seed has identical initial IDs across methods; acquisition seeds come from configuration. New trajectories train from their initial labels, then continue only their own checkpoints without borrowing another strategy's model.

Preparation checks equality of `initial_pair_ids`, `validation_panels`, `evaluation_sha256`, `training`, `source_revision`, `sar_backend`, and `shared_protocol`. The last binds the matched-sampling schema, fixed seeds, rounds, batch size, private data, and asset hashes. Selected history, training sets, and predictions naturally diverge after methods choose different candidates. Fairness requires consistent input definitions and update rules, not identical predictions in every round.

Input bindings define data sources and initial sets. The controller's fixed training/trajectory constraints are stated above; changing TOML alone does not establish another supported protocol. SAR five-fold benchmarks combine shared environment setup and training/inference entry points through [backends/catalog.json](../packages/tricycle-sar/src/tricycle_sar/backends/catalog.json). Sampling reuses the same backend. Reruns use the current backend and record actual method bindings.

Sampling calls SAR `get_backend("rxnresid")` directly: `training_command` for the first round, `refit_command` thereafter, and `inference_command` for predictions. The [SAR RxnResid adapter](../packages/tricycle-sar/src/tricycle_sar/rxnresid.py) supplies round configurations, revealed-label splits, checkpoint data updates, and the frozen route vocabulary. Sampling passes current data without independently constructing model commands or environment recipes.

New tasks use the [SAR sampling runtime](../packages/tricycle-sar/src/tricycle_sar/sampling_runtime.py) to deliver the validated source archive selected by current model assets and invoke the shared SAR initializer to inherit the image environment. `sar_backend` binds method ID, source revision, environment assets, and shared-interface hashes. Workers verify these before execution; recovery checks the same binding in the previous checkpoint. Stop on version/hash mismatches rather than falling back to old source or refreshing hashes to continue.

## Intelligent sampling

Intelligent sampling combines structural coverage, estimated cost, label feedback, and proposal review into one decision process, aiming to outperform all five static baselines at equal core-hour budgets. The main protocol remains `autonomous-core-hours-mae-v1`, with `guidance="coverage-cost-guided-v1"` and feedback/review enabled. Guidance protocol version 1.0.0 is defined in [cost_guidance.py](../packages/tricycle-sampling/src/tricycle_sampling/replay/cost_guidance.py). It adds no main model and does not change shared training/evaluation.

Each round recomputes novelty relative to sampled structures, local density, and structural workload across the full unselected pool, producing an initial intelligent proposal:

```text
c = product_count * mean_product_atom_count
initial_score = (0.5 * sampled_novelty + 0.5 * (1 - pool_local_density))
                  / max(1, c / median_pool(c))
cost_expression = estimated_core_hours
```

The median is calculated over the full pool and recorded as `context.coverage_cost.cost_proxy_median`; estimated core-hours are calibrated from revealed durations. The first acquisition decision should favor this reference formula, with concrete adjustments supported by current evidence. Its action is `replace`; later actions may be `retain`, `adjust`, or `replace`. The agent may change coverage weights or cost sensitivity, or add regional error/uncertainty terms with support counts, explaining tradeoffs when departing from the reference. The reference guides decisions and comparisons; the controller neither overwrites agent rankings nor mandates fixed weights.

Feedback comes from fixed auxiliary panels, the previous hypothesis, residuals for newly revealed labels before updating, and regional support counts. After executing the proposal across the full pool, compare its batch against the reference for novelty, sparsity, estimated core-hours, ties, substrate coverage, and selection overlap. At most one scientific review follows. Increased cost or reduced coverage needs an expected-benefit explanation. Do not revert to maximum uncertainty merely because variance fields exist, or change rules merely to produce a new formula. If the previous formula becomes constant on the shrinking pool, record only an ID-tiebroken counterfactual diagnostic; this must not block a valid new proposal.

`acquisition-guidance.json` stores the version, default formula, current input hashes, complete reference ranking, reference IDs, and diagnostics, with a summary in the decision materials. `formula-assessment.json` and `proposal-review.json` preserve proposal/reference differences. Expression auditing independently recomputes the reference. No MAE benefit is known for an unexecuted batch; batch diagnostics are not causal evidence.

The agent chooses weights, cost sensitivity, and model signals from revealed feedback. The initial proposal is a starting point and diagnostic reference, not an additional comparison curve. GT metrics are excluded from round acquisition; new matched trajectories establish final performance.

## Shared acquisition inputs

Both baseline execution entry points use `select_experiment_candidates()` with protocol `tricycle.matched-sampling/v1`. The five methods receive the shared six-dimensional structural descriptors, predictive means/variances for the same prior routes, and reference structures for pairs with revealed valid labels. The allowlist excludes target labels, future actual costs, unselected success masks, and GT metrics.

The six dimensions count reactant heavy atoms, heteroatoms, rings, aromatic atoms, rotatable bonds, and hydrogen-bond acceptors, with fixed transform `count/(1+count)`. Strategy use is frozen in advance: predictive uncertainty and A Score sum route variances; UCB and KB use epistemic variance of the route mean, `sum(v_epi)/n²`.

Bayes-UCB therefore consumes RxnResid epistemic variance directly without another main predictive model. It uses a route-independent Gaussian approximation and consistently favors lower predicted energy; report the difference from an overall MAE objective. KB adds structural RBF correlation with length scale 1 to the same marginal means/variances and conditions sequentially. This correlation is not a native RxnResid output. Temporary within-batch labels never enter formal training data.

LHS fixes its total design size after the initial batch, seed, and bounds. Each round's offset equals the number of consumed design points; do not reset bounds using the remaining pool. Discrete mapping and batch prefixes do not guarantee strict stratification. Missing shared predictions require stopping to complete inputs, not silently switching to random.

## Labels, evaluation, and auxiliary feedback

Training reveals proxy labels only for selected substrate pairs. Failures and missing valid labels are recorded only after selection. Structures and prior routes may be provided in advance. Unselected historical energies, outcomes, and actual durations remain private to replay; GT labels and performance remain on the evaluation side.

The current protocol evaluates the main model on the same fixed candidate space, so an increasing share of evaluation substrate pairs has entered training as sampling progresses. Proxy and GT energies may have different sources; structural overlap alone is not GT-label leakage. The primary metric is overall MAE on the fixed candidate space, not directly generalization error on entirely new substrates. Each run should separately report data rows, label coverage, and structural overlap from its frozen data.

Each seed chooses an eight-pair auxiliary panel from initially valid labeled pairs by fixed hash. Methods sharing a seed use the same panel. Auxiliary models retrain from scratch each round with the panel excluded. Main models still use all revealed labels and the common continuation rule. Eight pairs provide small-scale proxy feedback, not full-pool accuracy estimates or a main-model GT holdout test.

Residuals for newly revealed labels use the previous checkpoint's predictions before this round's training. They describe selected-sample difficulty, not generalization gains or causal effects. Without valid new labels, record missing residuals or reuse checkpoints under the shared rule; do not fabricate residuals. Evaluation metrics do not trigger stopping, elimination, or strategy switching.

## Core-hour budgets and statistics

New configurations use `decision_protocol.name="autonomous-core-hours-mae-v1"`, targeting GT MAE at a given cumulative proxy-computation cost. Evaluation nodes come from `decision_protocol.evaluation_budgets_core_hours`; they are not one universal allowance across spaces and do not guarantee that a batch with unknown actual duration stays within budget.

At each core-hour node, use the latest completed checkpoint within budget for each seed. Do not choose historical best MAE, use over-budget points, or extrapolate past actual coverage. Paired differences require the same seed and core-hour budget. Primary comparisons require common seed coverage. Monitoring plots may show incomplete trajectories but must identify available seed counts per point; missing values are not zero, and completed-seed means do not represent complete experiments.

Intelligent cost estimates are calibrated only from revealed durations and prior structural proxies. Missing valid calibration blocks acquisition; future actual costs must not be borrowed. Cost expressions produce positive estimates for audit. The MAE protocol does not require dividing acquisition scores by cost. Baselines retain their fixed selection rules with shared core-hour evaluation and accounting.

`cumulative_sampling_cost_core_hours` represents historical proxy quantum-chemistry cost, not new SAR job charges. Report SAR training/inference seconds and decision-model call times separately, including rejected proposals. Summed stage seconds are neither elapsed experiment time nor GPU-hours. Report actual devices, queueing, failures, and missing durations separately.

`intelligent-comparison.json` evaluates the final objective. At every preset core-hour node, pair intelligent sampling against each of the five static baselines by seed and require all five-seed mean MAE differences to be negative. Passing every node yields `passed`; after complete evaluation, a tie or failure to beat any baseline yields `not-met`. Missing complete seed/cost coverage for any method yields `pending`. This criterion describes average performance, not statistical significance; incomplete coverage cannot establish success. Evaluation is reporting-only and does not feed current acquisition or trigger strategy changes.

The older `autonomous-mae-v1` uses sample budgets and retains its original configuration and evaluation definitions; do not relabel it as the core-hour protocol. Even trajectories ending with every candidate selected may differ in accuracy because of training order. Preserve full budget trajectories.

## Local preparation

First verify the local readiness records, prior structures, frozen labels, evaluation, model source revision, and assets referenced by configuration. Example paths point to existing archives. Missing inputs cause preparation failure; data and models are neither replaced nor downloaded automatically.

```bash
uv run --package tricycle-agent python -m tricycle_sampling.replay.suite prepare \
  --config configs/dpa-unified-sampling-suite.toml
uv run --package tricycle-agent python -m tricycle_sampling.replay.suite status \
  --run-dir /absolute/suite
uv run --package tricycle-agent python -m tricycle_sampling.replay.suite tick \
  --run-dir /absolute/suite
```

Without an explicit directory, runs use local-time `runs/YYYY-MM-DD/HH-MM-SS.ffffff±ZZZZ/<suite-id>`. `prepare --run-dir` selects a new empty directory. These default commands perform local reads/writes only: no credentials, network access, decision-model calls, or submissions.

Inspect `suite.json`, `suite-hash.json`, `preflight.json`, and each method's `experiment.json`. Verify methods, trajectories, task counts, and concurrency limits against the generated plan. Under the current controller, `M` methods imply `5M` trajectories and at most `200M` round tasks. These are planning limits, not actual submission counts or charges. A task may fit both main and auxiliary models.

Suite requests must explicitly supply nonempty, unique `sampling_strategies`. Preparation records the matched schema; omitted fields do not imply an experiment set.

## Remote execution and supervision

Within existing experiment-execution authorization, use:

```bash
uv run --package tricycle-agent python -m tricycle_sampling.replay.suite watch \
  --run-dir /absolute/suite --live --allow-submit
uv run --package tricycle-agent python -m tricycle_sampling.replay.suite service-file \
  --run-dir /absolute/suite --allow-submit
```

`--live` enables service queries, downloads, and intelligent decisions; `--allow-submit` enables subsequent submissions. `service-file` generates a standalone `supervisor.plist` only. Install and start the system service separately; preparation does not mean it is running.

For each method, seed00 completes three evaluation rounds before its other four seeds start. This is an execution-validation gate, not selection by GT performance. Rounds within a trajectory run sequentially, while seed failures are isolated. All tasks create/reuse one Bohr job group under a shared file lock, with durable submission receipts. Bounded node-failure rescheduling follows the frozen job spec rather than blindly retrying submission APIs.

Source, configuration, data, worker, checkpoint, and input/output hashes are bound during preparation or their relevant stages. Mutable source/configuration snapshots use independent files so workspace edits cannot propagate to historical archives. Immutable large assets may share links. Hash mismatches stop execution; bindings must not be refreshed to continue an old experiment.

## Decision and run artifacts

| Artifact | Purpose |
| --- | --- |
| `suite.json` / `preflight.json` | Methods, shared protocol, initial-set consistency, and task limits |
| Per-method `experiment.json`, per-seed `state.json` | Frozen conditions, trajectory phase, selected sets, pause/failure state |
| `status.json`, `REPORT.md`, `evaluation-curves.json` | Local overview and completed evaluation points; excluded from acquisition inputs |
| Baseline `baseline-ranking.json` / `baseline-selection-request.json` | Deterministic execution, source, shared inputs, scores or KB steps, and request bindings |
| Intelligent `ACQUISITION.md` and function/response records | Expressions, hypotheses, expected effects, context, and evidence |
| Proposal/review responses and approval records for intelligent and historical reviewed runs | Proposals, actual batch diagnostics, at most one review, and final bindings |
| Submission receipts, download manifests, worker outputs, and checkpoints | Remote handles, input/output integrity, and within-trajectory model chains |

Intelligent responses include `expression`, `cost_expression`, `rationale`, `author_notes`, `decision_action`, `hypothesis`, and `expected_effect`. Actions are retain/adjust/replace. Retain must preserve both prior expressions verbatim. Constant full-pool scores are rejected when selection remains discretionary. When all remaining candidates must be selected, record deterministic selection and skip the decision-model call.

Each round allows at most three correction attempts; strategies with review allow at most one scientific review per attempt. Preserve failed records. Authentication failures block separately rather than retrying as scientific errors. Before completing a decision, verify terminal prior-round computation, downloaded results, and complete candidate coverage. Scientific discretion does not replace integrity checks.

## Pause, continuation, and failures

Pause and resume individual methods with:

```bash
uv run --package tricycle-agent python -m tricycle_sampling.replay.campaign pause \
  --run-dir /absolute/suite/<variant>
uv run --package tricycle-agent python -m tricycle_sampling.replay.campaign resume \
  --run-dir /absolute/suite/<variant>
```

Pausing does not terminate remote tasks. On handoff, first read frozen configuration, seed states, submission receipts, process records, and existing selections. Reuse completed computations and decisions; do not resample or submit duplicates. Authentication repair resumes only the unfinished decision phase. If supervision exited as blocked, restore the relevant supervisor after repairing state. Lock-file presence does not prove process liveness.

**Uncertain submissions:** A timeout does not prove that no task was created. Preserve inflight state and query the platform using the complete task name, time, project, and group, checking recent records. After verifying ownership of a unique match, complete the receipt and continue monitoring. Multiple matches or incomplete queries require further reconciliation without clearing markers. A single recovery attempt after confirmed non-submission must retain original selection, inputs, and job spec, and preserve query/recovery evidence.

The following tools have specific scopes and are not universal automatic repairs:

| Tool | Scope and recovery constraints |
| --- | --- |
| `tricycle_sampling.replay.watch` | One seed of intelligent or historical autonomous/feedback/reviewed; local reads by default, live requires an archived recovery record; preserves original locks, hashes, and submission guards |
| `tricycle_sampling.replay.recover_review` | Current blocked reviewed round; when a new proposal passes normal budget validation, permits an old constant formula only for explicitly labeled counterfactual batch diagnostics, completing the originally allowed single review |
| `tricycle_sampling.replay.recover_retain` | Last completed response explicitly says retain, and its shorthand exactly matches old expressions in full-pool scores, positive costs, and complete ranking; restores original text with evidence, without another proposal attempt |

Specialized recovery starts with read-only checks. When applicable, use explicit `--live` within original authorization. Preserve rejected records, counts, and current implementation hashes. Do not change accepted selections or reset correction limits. Baseline failures are addressed through their missing inputs or deterministic execution errors, not intelligent-formula recovery tools.

Read-only examples:

```bash
uv run --package tricycle-agent python -m tricycle_sampling.replay.watch \
  --run-dir /absolute/suite --variant intelligent --seed 1
uv run --package tricycle-agent python -m tricycle_sampling.replay.recover_review \
  --decision-dir /absolute/current-blocked-decision
uv run --package tricycle-agent python -m tricycle_sampling.replay.recover_retain \
  --decision-dir /absolute/current-blocked-decision
```

Round paths must point to the actual current blocked decision. Single-seed live continuation additionally requires `--recovery-record /absolute/archived-blocked-state.json --live --allow-submit`; it does not automatically clear blocked state. For advanced or completed trajectories, verify archives, selection, and checkpoint chains before deciding whether continuation applies.

## Audit scope

Plans/results for the five baselines retain `sampling_protocol`, `sampling_details`, and `sampling_experiment`. Experiment controllers also bind source, inputs, and selection requests. Final verification should reproduce selected IDs from frozen inputs under the original protocol version and check exclusions, real-label reveals, worker hashes, model chains, evaluation, and core-hours per round. A source DOI alone does not establish correct execution.

`tricycle_sampling.replay.audit` independently recomputes intelligent expressions, the intelligent default reference, and worker lifecycles. It supports intelligent and original intelligent-acquisition records. `--require-complete` checks planned-task and decision coverage, but **is not yet a complete independent selection audit for all five new baselines**. General status checks or partial audits do not establish final acceptance of all six methods.

Audits use the current package implementation; there is no entry point that loads historical controller source. Preserve existing results and original bindings. Reject mismatches explicitly rather than rewriting old hashes.

Platform auditing uses `tricycle_sampling.replay.platform_audit`. It reads `--snapshot` by default; only explicit `--query` accesses the platform. Establish complete listing coverage and check canonical Bohr IDs, names, the shared group, and duplicate/extra jobs. Full acceptance follows suite plan counts, not the old fixed three-method count. Current queries return at most 1000 items per page, so larger plans may exceed their scope. `has_more=true` prevents a completeness claim; the first page is not the whole list. Platform and local execution audits are not substitutes for each other.

## Figures and result archives

Run reports, scores, figures, and reproduction records belong only in Git-ignored `reports/`, `runs/`, or `artifacts/`, outside maintained documentation and source commits.

New unified suites use `analysis.sampling.replay_sampling_panels`, which supports literature baselines. Export a new fixed snapshot, then render in the reporting environment with matplotlib:

```bash
uv run --project analysis --locked python -m analysis.sampling.replay_sampling_panels \
  --run-dir /absolute/suite --out-dir /absolute/new-snapshot-directory
uv run --project analysis --locked python -m analysis.sampling.replay_sampling_panels \
  --snapshot /absolute/new-snapshot-directory/snapshot.json \
  --out-dir /absolute/new-snapshot-directory
```

The second command needs an interpreter with project dependencies and matplotlib. It exports PNG, SVG, PDF, and `figure-manifest.json`. Rendering verifies snapshot aggregation; the figure manifest binds snapshot, renderer source, and output hashes. Updates to existing results require a new snapshot directory.

Figures arrange proxy spaces actually present: GT MAE above, paired MAE differences at equal seed/cost below. New collections without random use predictive uncertainty as the reference; historical snapshots containing random retain their original reference. Static methods use dashed lines, intelligent methods solid lines. Bands show sample standard deviations, not confidence intervals. Incomplete trajectories retain available seed counts; fewer than five seeds use thin lines, single seeds have no band, and curves never extrapolate.

Final reports state shared conditions, protocol/source/hashes, sources and adaptations, per-seed completeness, supported budget ranges, costs and execution overhead, recovery history, and audit coverage. Explicitly state scope when full acceptance is unavailable.

## Full-trajectory worker entry point

For five-literature-baseline comparisons submitted one trajectory per worker, use [rxnresid-matched-sampling.toml](../configs/rxnresid-matched-sampling.toml):

```bash
uv run --package tricycle-agent python -m tricycle_sampling.active_learning validate \
  --config configs/rxnresid-matched-sampling.toml
uv run --package tricycle-agent python -m tricycle_sampling.active_learning prepare \
  --config configs/rxnresid-matched-sampling.toml
```

Trajectory seeds, rounds, acquisition seeds, and training parameters come from the request; model source comes from current SAR assets. `--rxnresid-source` may verify a clean local checkout, but delivery still uses the package's validated archive. Preparation does not clone or upgrade. One worker executes one trajectory, so task counts, resources, and artifacts differ from the round-based suite. Both reuse the same acquisition interface and SAR backend. Merging results still requires checking data, training, evaluation, and execution definitions.

Existing experiments retain configurations, bindings, and results without maintaining executable historical implementations. New matched experiments use new directories and versions. Old round results, checkpoints trained for different epoch counts, or archives with rewritten hashes cannot stand in for new-protocol results.
