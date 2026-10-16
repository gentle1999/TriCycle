# tricycle-sampling

`tricycle-sampling` selects the next reaction batch from an explicit candidate pool and provides preparation, supervision, decisions, and auditing for multiround SAR sampling replay. Model training and inference use shared `tricycle-sar` interfaces; remote jobs run through an injected Bohr adapter.

## Replay entry points

Use `python -m tricycle_sampling.active_learning` to prepare, submit, and collect a complete sampling experiment. `space_snapshot.py` validates and generates space snapshots containing only data and adaptation evidence, without controller source copies. Submission calls the injected Bohr gateway directly. An experiment shares a job group and submission lock, reuses existing receipts, and retains an uncertainty marker that blocks automatic resubmission when submission outcome is unknown.

Prepare a standalone agent-authored acquisition inference worker with `python -m tricycle_sampling.acquisition_worker --prepare-bundle <directory>`, which delivers the package and `run.sh`; then add the request, checkpoint, and input table. Bohr runs `bash run.sh <request.toml>`. Model-signal generation, integrity checks, and validation-set metrics execute inside the package.

Database export/substrate adaptation use `python -m tricycle_reaction_db.pools export|adapt`; offline distribution statistics use `analysis.sampling.reaction_collection_distribution`.

Runtime implementation lives in [replay/](src/tricycle_sampling/replay/) and depends on neither repository `tools/` nor `tricycle-exploration`. The basic sampling API depends only on core; replay controllers use optional dependencies in `tricycle-sampling[replay]`. Workspace commands:

```bash
uv run --package tricycle-sampling --extra replay python -m tricycle_sampling.replay.campaign --help
uv run --package tricycle-sampling --extra replay python -m tricycle_sampling.replay.suite --help
```

| Responsibility | Package entry point |
| --- | --- |
| Round-by-round supervision of one experiment with multiple seeds | `tricycle_sampling.replay.campaign` |
| Unified suite spanning methods | `tricycle_sampling.replay.suite` |
| Remote single-round training/inference | `tricycle_sampling.replay.worker` |
| Remote complete baseline trajectory | `tricycle_sampling.trajectory_worker` |
| Structural signals and RDKit analysis | `tricycle_sampling.structure_analysis` |
| Status, auditing, and recovery | `tricycle_sampling.replay.report`, `audit`, `platform_audit`, `watch`, `recover_review`, `recover_retain` |
| Bilingual, subagent-authored blocked-state email notifications | `tricycle_sampling.replay.alerts`, `alert_author`; SMTP transport in core |

`campaign` directly calls `replay.selection.select_round`, validates model evidence, performs deterministic selection, and atomically saves round records. SAR staging delivers the current package to remote inputs; launchers use `python -m` without copying individual tools scripts. Offline plotting entry points are in `analysis.sampling`.

Default prepare/status operations touch only local files. Live supervision requires `--live`; submission additionally requires `--allow-submit`. Existing locks, budgets, retries, and submission-receipt safeguards remain active. New runs bind current packaged implementation hashes. Recovery verifies original bindings and rejects silent continuation after source changes; it neither refreshes old run hashes nor loads historical source.

Blocked-state email is opt-in via `TRICYCLE_TRAJECTORY_ALERTS_ENABLED` and separate
notification configuration. SMTP host/protocol/addresses/credentials come from
environment variables. A reporting-only subagent authors bilingual email, while
core performs delivery to configured recipients. Deduplication, bounded retries,
and compact notification records live outside runs under `.tricycle/alerts`.
See [trajectory alerts](../../docs/trajectory-alerts.md) for dry runs, independent
observation of existing runs, and retry recovery. Notification errors do not alter
the trajectory, sampling inputs, or scientific failure counters.

Each seed trajectory owns one authoritative `state.json` for resumption and one
`state-events.json` journal for operational transitions, failures, and recovery.
Ordinary polling updates the current state without adding a history snapshot or
journal event. Events contain only control fields, not cumulative selected IDs,
decision context, or worker assets. Neither file is copied into round submissions.
Round-specific receipts, scientific inputs, decisions, and results remain bound
to their rounds. Existing `history/*.json` evidence is preserved but no longer
extended. The journal is written before the state, so an interrupted save may
leave an event ahead of the authoritative state revision; that event is not proof
that the transition committed. Recovery always reads `state.json`.

`active_learning submit` receipts use top-level `run_id/status/external_id/request_sha256/job_group_id`. Recovery requires `status=submitted`, a nonempty string job ID, the same job group, and a matching request hash. Legacy fields, nested IDs, missing fields, and unknown fields are errors; original receipts remain intact and affected jobs are not resubmitted. `submission-inflight.json` blocks automatic resubmission after an unknown submission outcome.

New comparisons use five literature baselines, each tied to a different primary reference. The baselines and intelligent strategy share the experimental workflow, varying only the selection strategy. See [methods and references](../../docs/sampling-baselines.md) for literature evidence and [unified sampling experiments](../../docs/mae-intelligent-sampling.md) for operations.

## Comparison set and sources

`LITERATURE_BASELINE_STRATEGIES` defines the following order. Primary references document use in papers from the last five years; implementation records describe adaptations to TriCycle.

| Strategy ID | Primary reference | Current selection rule |
| --- | --- | --- |
| `predictive-uncertainty` | Zhong et al., *Nature Communications*, 2025, [DOI](https://doi.org/10.1038/s41467-025-59812-0) | Descending total predictive uncertainty |
| `latin-hypercube` | Slattery et al., *Science*, 2024, [DOI](https://doi.org/10.1126/science.adj1817) | Sequentially map fixed LHS design points to the nearest unselected candidate |
| `ucb` | Wang et al., *Nature*, 2024, [DOI](https://doi.org/10.1038/s41586-024-07021-y) | Descending upper quantile of a Gaussian reward posterior |
| `distance-uncertainty-product` | Shrestha et al., *Nature Communications*, 2024, [DOI](https://doi.org/10.1038/s41467-024-49011-8) | Distance to the nearest labeled reference multiplied by predictive variance |
| `uncertainty-kriging-believer` | Souza et al., *JACS*, 2025, [DOI](https://doi.org/10.1021/jacs.5c02218) | Sequential maximum-standard-deviation selection with posterior updates using temporary predicted labels within each batch |

`random`, `epistemic-uncertainty`, and `aleatoric-uncertainty` are standalone strategies outside this literature comparison set. A shared random initial set still initializes experiments.

[protocols.py](src/tricycle_sampling/protocols.py) is the sole code definition of sources and method protocols. All five literature protocol versions are `1.0.0`; implementation versions are recorded separately, currently `1.1.0` for predictive uncertainty and `1.0.0` for the other four. Literature protocol versions, strategy implementation versions, and experiment schemas serve distinct purposes.

## Unified experiment interface

[experiment.py](src/tricycle_sampling/experiment.py) provides protocol `tricycle.matched-sampling/v1`:

```python
experiment_sampling_request(
    strategy, candidates, *, reference_features, budget, seed, design_size, offset
)  # Returns SamplingRequest

select_experiment_candidates(
    strategy, candidates, *, reference_features, budget, seed, design_size, offset
)  # Returns SamplingResult
```

Both accept only the five canonical strategy IDs above. Round-by-round replay in [baselines.py](src/tricycle_sampling/replay/baselines.py) and complete trajectories in [trajectory_worker.py](src/tricycle_sampling/trajectory_worker.py) call this interface. The suite's intelligent strategy uses the acquisition-expression interface while sharing the same main model, candidate space, initialization, training, and evaluation workflow. It uses versioned coverage/cost initialization proposals, revealed model feedback, and one batch review. [cost_guidance.py](src/tricycle_sampling/replay/cost_guidance.py) supplies reference guidance; the agent-authored formula determines final ranking.

### Shared inputs

Each `Candidate.payload` supplies these fields. The adapter builds requests from an allowlist and does not copy target labels, unobserved success masks, future costs, or evaluation metrics.

| Field | Shared definition |
| --- | --- |
| `design_features` | Six reactant structural counts transformed by `count/(1+count)` |
| `product_count` | Positive integer route count for the substrate pair in the current frozen structural prior |
| `prediction_mean` | Mean RxnResid predicted energy over that same set of prior routes |
| `predictive_variance` | Sum of total predictive variances over those routes |
| `epistemic_variance` | Sum of epistemic variances over those routes |

The six dimensions are ordered as heavy-atom count, heteroatom count, ring count, aromatic-atom count, rotatable-bond count, and hydrogen-bond acceptor count. `reactant_structure_features()` is an explicit local RDKit adapter; general numerical sampling interfaces need not parse structures.

`reference_features` contains substrate-pair structures whose valid training labels have already been revealed. It must be nonempty, disjoint from current candidate IDs, and use the same six-dimensional transform. All five methods validate shared inputs, even though LHS does not use predictions for selection. Missing model coverage requires completing shared inference, never silently falling back to random selection.

### RxnResid variance in Bayes-UCB

For a substrate pair with `n` prior routes, route predictions `mu_j`, and epistemic variances `v_epi_j`:

```text
mu_pair  = sum(mu_j) / n
v_reward = sum(v_epi_j) / n²
s_reward = sqrt(sum(v_epi_j)) / n
score    = -mu_pair + Phi_inverse(0.95) * s_reward
```

The unified protocol treats RxnResid epistemic variance as uncertainty in expected energy and builds a Gaussian approximation assuming independent routes. `direction="minimize"` favors lower predicted energy. Total predictive variance includes aleatoric noise and does not automatically replace reward-posterior variance. This approximation does not estimate route correlations and is not the original bandit posterior of Wang et al.'s condition-generality task.

Predictive uncertainty and A Score use summed route variances; UCB uses the epistemic variance of the route mean. These strategy definitions are fixed in advance. Results must explain the distinction between UCB's preference for low energies and the overall MAE objective.

### Shared KB adaptation

The unified entry point injects `StructuralKrigingBelieverFactory`, constructing the following covariance from the same RxnResid marginal means and `s_reward`:

```text
K(i,j) = s_i * s_j * exp(-||features_i - features_j||² / (2 * ell²))
ell = 1
```

The structural RBF introduces an explicit correlation assumption; RxnResid does not supply this joint covariance. After each selection, the current predicted mean becomes a temporary label, and conditioning precedes the next standard-deviation calculation. Incremental conditioning avoids a complete candidate-pool covariance matrix. It adds no main SAR training jobs, and temporary labels enter neither the official training set nor the database.

Souza et al. use RF tree standard deviations and temporary within-batch refits. This implementation preserves sequential KB selection using a pair-level Gaussian adaptation; source records explicitly document the difference.

### LHS design across rounds

The unified protocol fixes six-dimensional `[0,1]` bounds, the acquisition seed, and total design size after the initial batch. `offset` counts consumed design points. Each round regenerates the same design sequence, resumes mapping at the offset, and excludes previous selections. Discrete candidate mapping and arbitrary batch prefixes do not guarantee strict Latin stratification.

The [experiment controller](../../docs/mae-intelligent-sampling.md) manages shared conditions, trajectory budgets, evaluation points, and recovery constraints. `sampling_experiment.shared_inputs_sha256` binds current candidates, predictions, and references; it should match when changing strategies on the same snapshot. It may differ once trajectory histories diverge.

Deterministic replay baselines use only these five strategies and the current `sampling_experiment` schema. Missing/unsupported schemas are rejected without falling back to older algorithms. Existing run records remain intact; new computations prepare new runs from explicit configurations. Other strategies in the general candidate API below remain independently available.

## General request interface

The plugin ID is `tricycle.sampling`, with public capabilities `candidate.validate`, `candidate.select`, and `strategy.register`. Python planning uses `SamplingPlugin.plan("select", request_mapping, context)`; execution uses `SamplingPlugin.select(request)`.

| `SamplingRequest` field | Constraint |
| --- | --- |
| `candidates` | A sequence of `Candidate` objects, each with unique `candidate_id` and `payload` |
| `budget` | Nonnegative integer no larger than the candidate count |
| `strategy` | Registered name; the general API defaults to `random` |
| `seed` | Optional integer; LHS and unified experiments require an explicit integer |
| `metadata` | Upstream context; baseline configuration resides in `metadata.baseline` |

`SamplingResult` returns `selected`, `remaining`, strategy, seed, and metadata, with `selected_ids` and `to_dict()` helpers. Candidate objects preserve their inputs.

### Standalone baseline parameters

This table applies to the general API. The unified experiment adapter freezes parameters; do not vary shared inputs by strategy.

| Strategy | Candidate fields | `metadata.baseline` |
| --- | --- | --- |
| LHS | Dense, finite `design_features` | Required `bounds`; `feature_field` can rename the field; `design_size` defaults to budget, `offset` to 0 |
| Bayes-UCB | `reward_posterior_mean`, nonnegative `reward_posterior_std` | `quantile` defaults to 0.95 and must lie in `(0.5,1)`; `direction` defaults to `maximize`, also accepting `minimize` |
| A Score | `design_features`, nonnegative `predictive_variance` | Required `reference_features`; `feature_field` and `variance_field` can rename fields |
| Default KB adapter | Joint posterior keyed by ID; see below | Required `posterior`; `observation_noise_variance` defaults to 0 |

These four strategies reject unknown baseline parameters. LHS matches the nearest unselected candidate using Euclidean distance after fixed-bound normalization. A Score uses Euclidean distance in the supplied representation, without additional normalization or within-batch reference updates. UCB scores `maximize` as `mu + Phi_inverse(q)*std` and `minimize` as `-mu + Phi_inverse(q)*std`.

The general predictive API accepts `predictive_variance`, `predictive_std`, or scalar `predictive_uncertainty`, in that priority order without numerical transformation. Epistemic/aleatoric strategies use corresponding prefixes. These fields have different score units; input providers must identify the quantity used. Explicit scores take precedence over `posterior_probabilities`. Binary probabilities produce natural-log entropy locally:

```text
predictive = H(mean(p_i))
aleatoric  = mean(H(p_i))
epistemic  = predictive - aleatoric
```

Classification probabilities and regression variances are separate input forms. RxnResid regression experiments use the variance inputs above. `UncertaintyScores.to_dict()` emits three `*_uncertainty` scalar fields. Legacy `_unc` spelling, bare component names, nested `uncertainty` tables, and `posterior_probs` are rejected. Density-weighted strategies read only `pair_density`.

### KB surrogate protocol

General `SamplingPlugin()` uses `GaussianKrigingBelieverFactory` by default. `baseline.posterior` contains `candidate_ids`, `means`, and `covariance`. IDs must be unique and exactly match the current pool; means and covariance follow that order. Covariance must be finite, symmetric, and positive semidefinite; numerical tolerances and singular matrices are allowed. Complete input storage is O(n²), and positive-semidefinite validation is O(n³), suitable for small pools and interface validation.

Inject other local surrogates with `SamplingPlugin(kriging_believer_factory=...)`:

- `KrigingBelieverFactory` provides stable `adapter_id` and `version`, local-input-only `validate(candidates, metadata)`, and `create(candidates, metadata, seed)`.
- `KrigingBelieverSession.predict(remaining)` returns complete ID-to-`SurrogatePrediction(mean, std)` mappings; `believe(candidate, predicted_label)` conditions or temporarily refits before the next score.
- Every selection creates a separate session. Never rewrite the main checkpoint, true labels, or upstream inputs. Record custom-refit seeds, failures, and cost explicitly.

`plan` validates KB inputs without creating sessions or fitting models. Unified experiments use the structural RBF adapter; standalone TOML examples use full Gaussian covariance. Label these separately.

## Audit and reproducibility

All five methods break ties by ascending candidate ID. Plans and results generate `metadata.sampling_protocol` containing paper, DOI, evidence location, rule, adaptation, protocol ID/version/hash, actual strategy version, seed, budget, and decision hash. Results also include `metadata.sampling_details` with effective parameters, scores, or within-batch KB steps. Requests cannot override either plugin-generated field.

The unified adapter generates `metadata.sampling_experiment`, recording representation, route aggregation, Gaussian/RBF assumptions, and the shared-input hash. Protocol hashes bind protocol content; decision hashes bind decision records. Experiment records separately bind controller source, data, and checkpoint file hashes.

Built-in `random` canonicalizes by ID before drawing through local `random.Random(seed)`, without changing global random state. `first-n` preserves input order.

## Examples and execution

The five [complete TOML examples](../../configs/sampling-baselines/) demonstrate the standalone API, not unified experiment configuration:

```bash
uv run --package tricycle-agent tricycle-agent run \
  candidate.select configs/sampling-baselines/ucb.toml --run-id sampling-ucb-demo
```

Selection itself connects to no database, Bohr, or model service. The application writes local plans, results, and traces to the run. Without an explicit directory, it uses `runs/YYYY-MM-DD/HH-MM-SS.ffffff±ZZZZ/<run-id>` in the local timezone.

New experiment configurations are the [round-by-round unified suite](../../configs/dpa-unified-sampling-suite.toml) and [complete worker trajectory](../../configs/rxnresid-matched-sampling.toml). Existing results and checkpoints remain intact. Reruns use current implementation in new runs, without migrating by refreshing old hashes.

## Agent-authored acquisition and custom strategies

`agent-authored-acquisition` accepts `metadata.agent_acquisition` and executes authored expressions in a safe numerical DSL. Upstream callers invoke models and prepare structural evidence. Supported contracts are `tricycle.agent-acquisition/v1` and the MAE protocol's `v2`, each with its own input constraints. v1 requires formulas combining novelty, model quality, sampling distribution, and cost. v2 allows evidence-based signal selection; its cost expression must remain a verifiable positive estimate but need not penalize the score.

Evidence must bind local, terminal, downloaded computation reports and artifacts. The DSL supports numerical fields, context, estimated_cost, basic arithmetic/comparisons/conditionals, and `abs`, `exp`, `log`, `log1p`, `max`, `min`, and `sqrt`. It executes no arbitrary Python and accesses no network, randomness, or system state. See [acquisition.py](src/tricycle_sampling/acquisition.py) and the [agent-authored acquisition skill](../../.agents/skills/agent-authored-acquisition/SKILL.md) for the full contract.

Additional engineering strategies include `pair-core-set`, `density-weighted-uncertainty`, and `uncertainty-diversity`; their rules are not automatically attributed to the five primary papers above. Requests accept only registered canonical names, not legacy paper names or alternate spellings.

Custom strategies implement `SamplingStrategy.name` and `select(candidates, budget, seed)`. Register through constructor arguments or `register_strategy()`; replacing a name requires explicit `replace=True`. New scientific rules need stable versions, input contracts, and source records. Separate injected adapters handle candidate export and external writes.

## Checks

```bash
uv run --frozen --all-packages pytest packages/tricycle-sampling/tests
```

Duplicate candidates, excessive budgets, unknown strategies, nonfinite scores, and incomplete posteriors fail requests. The basic selection API has no remote side effects. See the [unified experiment guide](../../docs/mae-intelligent-sampling.md) for recovery of package replay-controller jobs.

## Round asset storage and transfer

Apply [run record scope](../../docs/run-records.md): sampling runs contain execution decisions, state, result validation, and recovery evidence. New reusable frozen sampling spaces default to `artifacts/datasets/<date>/<timestamp>/<snapshot-id>/`; offline comparison reports belong in `reports/`. Explicit output roots and existing hash-bound recovery paths remain unchanged.

Replay configuration `asset_store` selects the project-level immutable store, defaulting to `artifacts/task-assets` under the working directory. Fixed RxnResid runtime, structural priors, vocabularies, and private data retain unique bytes only. Experiment `asset-references.json` records references; runs retain neither `assets/` and `private/` input directories nor symlinks.

Round CSVs and worker requests are generated in temporary directories, then stored by hash. `input-assets.json` references the complete delivery, including round inputs, fixed runtime, evaluation data, and the previous checkpoint from the same trajectory. `input-assets-hash.json` binds that manifest. Runs retain no `input/` directory; reads consistently use `input_file`. Worker requests still bind per-file hashes for the final complete input. At submission, the Bohr adapter temporarily materializes all inputs and cleans up after success or failure. The CLI still uploads required fixed files per job and does not claim cross-job remote caching.

After verification, `result/` and `download/` also use shared storage, retaining original download archives, logs, checkpoints, original paths, and per-file checks. Identical checkpoints in multiple output locations occupy one physical copy. Recovery and audit verify shared references, complete inputs, and the previous-model chain together. Source drift is rejected by default; old experiment bindings are not refreshed.

Only after explicit user authorization to continue, item-by-item confirmation that changes are limited to storage adaptation, and verification of input-byte equivalence may an independent `storage-compatibility.json` audit receipt be written in the experiment directory. Schema `tricycle.replay-storage-compatibility/v1` records the original `experiment_sha256`, `authorization`, `reason`, `evidence_path`/`evidence_sha256`, and `before`/`after` hashes for every original controller path in `changes`. Original configuration and historical manifests remain unchanged. Unlisted files retain original hash validation, and subsequent drift is rejected again. This receipt does not cover algorithm, data, training, or evaluation protocol changes.

Decision validation errors automatically invoke the configured agent again with a repair task: the concrete error, an explicit `last_accepted_round_formula`, and a separately labeled rejected proposal including its declared action. Repair preserves scientific intent and valid fields; the agent returns a complete corrected response, which undergoes full-pool validation and the configured review before execution. `retain` always refers to the last accepted round, including during review and repair. The controller does not silently rewrite formulas or relabel actions. Failed outputs remain under `rejected-attempts/`, and the frozen `max_decision_attempts` bounds automatic repair.

An explicitly authorized repair of an active decision controller may use `decision-controller-amendment.json` with schema `tricycle.decision-controller-amendment/v1`, the unchanged `experiment_sha256`, `authorization`, `reason`, `evidence_path`/`evidence_sha256`, and per-file `changes` containing `before`/`after` hashes. Only `replay/campaign.py`, `replay/mae.py`, and `replay/decision.py` are eligible; review their actual diff and preserve the original source as audit evidence before authorizing. This receipt records a protocol intervention, not storage equivalence or a pure language comparison. Original experiment/suite hashes remain unchanged; unrelated source changes or subsequent drift fail verification.

For an exhausted decision, explicit recovery authorization can add at most three correction attempts for that task via `decision/recovery-authorization.json` (`tricycle.decision-recovery/v1`). Required fields are `task_sha256`, `original_maximum`, `additional_attempts` (integer 1–3), `authorization`, `reason`, and `original_rejections`, mapping original one-based attempt numbers as strings to their `rejection.json` hashes. Preserve counters and archives; additional attempts continue their numbering. Pause supervision and acquire seed-operation locks before applying a repair or resuming only the affected blocked stages. Record the intervention and recovery outcomes in retrospective comparisons.

Workers place training temporary files in `work/round/`; download selectors include only `outputs/round/`. An explicit output list publishes one main-model weight file, auxiliary-model weights, predictions, metrics, generated configuration, and necessary training logs/summaries. Previous uploaded weights, `refit-input.pt`, raw/preprocessed training data, and duplicate main-model weights are excluded.

Replay timeouts retain bounded restart opportunities. A failed agent invocation is terminated before retry; its files go to `decision/timeout-attempts/proposal/` or `review/`. Each stage permits three timeout restarts, separate from scientific correction attempts. Review restarts preserve the validated proposal. Authentication errors do not enter this path. Status/download operations retain their established adapter retries and controller backoff.

A submission timeout remains in `submit` with its inflight marker and error. The Bohr adapter's read-only `reconcile_submission(name, group_id)` requires an explicitly complete group inventory, an exact stable job name, a unique match, and a public job ID. A match restores the receipt without resubmission. Absence must be confirmed twice at least 60 seconds apart before archiving the marker under `submission-timeouts/` and permitting another submit. At most three resubmissions are allowed per round. Incomplete inventories, ambiguous matches, or unclassified interruptions never authorize resubmission; evidence stays recoverable. Inventory checks are saved under `submission-reconciliation/`. New submissions still use `submit_retry_mode=none`, so there is no blind API retry loop.

For authorized timeout-controller amendments, preserve the preceding amendment receipt and its audit evidence before updating the source-bound receipt. Its optional `bohr_gateway_sha256` also binds the modified reconciliation adapter. Original scientific configuration and worker assets remain unchanged; comparison reports disclose these execution interventions.

When no new labels exist and the previous model is reused, workers record input-weight hashes in `output_references` without returning weight bytes. After verifying input bindings for the same trajectory, the controller restores those references in the local result view. Failures still publish available diagnostic logs.

## Portable replay results

`replay.published` owns scientific export and verification of replay observation
packages. It checks source bindings, prediction identity, metrics, incremental
acquisitions, and cost accounting. `analysis/sampling/published_report.py` owns
offline tables and figures consuming those verified observations. Verification
requires no original run tree or shared model assets after publication. See
[reproducible results](../../docs/reproducible-results.md).

## Live replay records

New replay seeds use a native SQLite record namespace throughout execution.
`replay.runtime_records` owns local placement, exact-byte migration, snapshots,
and inspection; `replay.agent_records` owns durable invocation intent, streamed
logs, and external handoff recovery. See [live replay records](../../docs/compact-replay-records.md)
for environment configuration, source-bound amendments, and recovery commands.
Use `tricycle_core.record_io` for logical paths; ordinary `Path` I/O cannot read
keys inside a native seed container. Scientific inputs and checkpoints remain
verified shared assets. Existing scientific algorithms and retry budgets apply.
