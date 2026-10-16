---
name: agent-authored-acquisition
description: "Analyze reaction structures with RDKit, exploration history, estimated cost, and SAR model quality, then author and record a deterministic acquisition function for one sampling round."
---

# Agent-Authored Acquisition Functions

Use English for analysis, decisions, formulas' explanations, delegated tasks, and new narrative records even when the user writes in Chinese, unless the user explicitly requests another language. Pass that language choice to every delegated agent. Existing evidence remains unchanged.

## Select the round protocol first

Read `metadata.agent_acquisition.schema`, or replay `task.json`, `response-schema.json`, and experiment configuration. A particular experiment recipe is not a global skill default.

Replay `decision_protocol` rejects unknown fields. `feedback` and `review` are booleans. Core-hour evaluation budgets and `guidance` apply only to the core-hour protocol. Protocol name determines `objective`; persisted values must agree. Correct misspellings/conflicts rather than silently disabling feedback/review.

| Context | Response and scientific constraints |
| --- | --- |
| Generic `exploration.round`, `tricycle.agent-acquisition/v1` | Expression must reference novelty, sampled distribution, model metrics, and cost; cost must use product count and mean product atom count |
| `autonomous-mae-v1` / `autonomous-core-hours-mae-v1`, acquisition schema v2 | May retain/adjust/replace; return seven scientific fields under the task schema. Core-hour mode uses calibrated core-hour costs, without v1 mandatory feature references |
| Compact replay without those autonomous protocols | Return formula, cost, rationale, and analysis under the generated response schema. Input-brief format version is not acquisition-protocol version |

Generic QM rounds are recorded by `exploration.round`; historical-label replay uses package `tricycle_sampling.replay.selection.select_round`. Both share sampling/evidence validation without depending on one another's controllers. When replay has already generated and verified structural evidence, review the supplied materials without rerunning structure tools.

## Intelligent sampling

The matched suite compares five literature baselines with intelligent sampling. Intelligent uses `autonomous-core-hours-mae-v1` with `guidance="coverage-cost-guided-v1"` and both feedback/review true.

Its initial proposal combines coverage gaps, pool sparsity, and expected cost:
`(0.5*sampled_novelty+0.5*(1-pool_local_density))/max(1,c/median_pool(c))`,
where c is product_count×mean_product_atom_count. The controller writes a complete legal expression, median context, reference batch, and expected core-hours to acquisition-guidance.json. Calibration uses revealed timings only.

Initially prefer the reference or justify an adjustment. Subsequently retain effective rules or use feedback to adjust weights, cost sensitivity, region terms, or uncertainty. Explain coverage and benefit-per-core-hour tradeoffs when departing from the reference/previous rule. Do not default back to pure maximum uncertainty or change effective rules merely to produce a new formula.

Use fixed auxiliary-panel changes, pre-update residuals on newly revealed labels, and regional support counts. At most one scientific review compares actual/reference proposals by novelty, sparsity, expected core-hours, coverage, and selection overlap. The reference is guidance, not mandatory weights or an override of agent ranking. Performance evaluation is not a round input; unknown labels, future costs, and GT metrics remain isolated.

The objective is to outperform all five static baselines at all preset core-hour budgets. Report incomplete paired coverage or unmet objectives honestly; batch diagnostics do not prove MAE improvement. See the [matched-experiment guide](../../../docs/mae-intelligent-sampling.md) and [cost_guidance.py](../../../packages/tricycle-sampling/src/tricycle_sampling/replay/cost_guidance.py). The autonomy below still applies: this guidance adds a starting proposal/departure explanation without changing safe DSL, evidence, or bounded correction.

## Core-hour objective: explicitly selected by the experiment

The objective is **minimize independent ground-truth evaluation-pool MAE at a given cumulative proxy-computation core-hour budget**. Configure `decision_protocol.name = "autonomous-core-hours-mae-v1"` and preset `evaluation_budgets_core_hours`; the interface objective is `minimize-ground-truth-mae-at-core-hour-budget`. Batch count is execution granularity, not the optimization budget or primary evaluation axis.

- Review consumed core-hours, evaluation nodes, remaining budget, and candidate estimates. Explain resource tradeoffs, while retaining freedom over the complete ranking without mandatory penalties, novelty terms, weights, or diversity quotas.
- Calibrate `candidate.estimated_core_hours` only from selected-pair timings and prior structural workload. `cost_expression` uses that field or `context.core_hour_budget.calibration.core_hours_per_proxy_unit` and returns core-hours. Uncalibrated structural proxies are not core-hours.
- GT labels, global/regional errors, trends, and threshold hits never enter agent input, training, or control branches. Auxiliary validation and pre-update residuals on sampled proxy labels are allowed but do not establish GT benefit.
- Retain is allowed. Inspect tied-score counts, cutoff ties, ID-order effects, batch structural coverage, and estimated cost. These are diagnostics, not new scientific priors. Missing failure evidence does not establish efficiency.
- Main comparisons use the latest budget-eligible completed checkpoint at equal core-hour budgets with observed coverage for all matched seeds. Do not select the lowest historical MAE, interpolate, extrapolate, or adapt rules from GT. Record training/agent overhead separately.
- Evaluation nodes do not guarantee hard spending limits under unknown actual runtimes. Over-budget checkpoints are excluded at that node. Preserve configured training epochs and auxiliary-validation recipe.

See the [core-hour experiment guide](../../../docs/mae-intelligent-sampling.md). Core-hour and sample-count protocols share safe DSL, evidence, seven-field responses, and correction boundaries; the selected protocol defines the budget objective.

## Autonomous MAE with a sample-count budget

For new experiments explicitly targeting lower MAE, `decision_protocol.name = "autonomous-mae-v1"` uses `tricycle.agent-acquisition/v2` and a model-written full acquisition rule. Existing trajectories are not migrated automatically. This protocol requires neither particular features nor a new formula every round; v1 feature constraints apply only to the generic v1 context above.

Both protocols require complete evidence, actual RDKit structures, safe numerical DSL, full-pool evaluation, finite scores, positive costs, and terminal-calculation/download barriers.

- Minimize generalization MAE within the sampling budget. Costs record/manage budget; the controller does not force a cost penalty.
- Choose `retain`, `adjust`, or `replace`. Retain preserves the previous expression/cost_expression byte-for-byte and is invalid for the first decision. Select or discard available signals with evidence; do not change formulas for novelty alone.
- Return expression, cost_expression, rationale, author_notes, decision_action, hypothesis, expected_effect. Hypotheses/effects should be testable in later rounds. Difficult-sample residuals are not proven generalization gains.
- The controller generates available features from real structures/revealed labels; do not invent unencoded chemical priors. Comparable auxiliary validation uses the experiment's fixed initial labeled-pair panel and always excludes it from auxiliary training. Main training still uses all revealed labels, so auxiliary errors are not main-model holdout errors. Record panel size without claiming global representativeness.
- Feedback additionally supplies fixed-panel changes, previous hypothesis, previous-main-checkpoint pre-update residuals on newly revealed labels, grouped errors, and support counts. It reads no unsampled labels/costs or final GT.
- Review first validates the proposal over the complete pool, then supplies batch features, regional concentration, overlap with the previous rule, and representative-route RDKit similarity. Permit at most one scientific review and either retain or revise; no mandatory diversity prior.
- Reject constant scores when selection choices remain. If budget selects every remaining candidate, validate evidence and record `decision_source = "controller-all-remaining"` directly, without a model call or a claim of agent ranking.
- `max_decision_attempts` defaults to three including the first. Each review-enabled attempt permits at most proposal/review calls. Preserve raw proposals, reviews, errors, and final responses.
- Preparation binds current sampling implementation/worker-input integrity in experiment records. Inspect implementation changes before continuation; do not silently continue old experiments or preserve historical controller source copies.

See [MAE experiments](../../../docs/mae-intelligent-sampling.md) for parallel execution/recovery.

## Scope and execution boundary

Use when substrate-pair priors have expanded into the complete possible-product pool and a round needs a formula informed by sampled coverage, estimated next-batch cost, and current SAR quality. Author or retain a protocol-compliant rule from the actual trajectory rather than reducing this capability to a fixed uncertainty baseline.

Generic QM decisions use `exploration.round`; replay controllers record replay decisions. Local decisions call neither database nor Bohr adapters. Structural evidence comes from the tools below or validated replay preparation. Formulas are safe numerical expressions, never ad hoc executable Python. Acquisition and submission are separate phases: do not decide/submit the next batch while current calculations run, or submit new candidates within the same computation job.

## Prepare model evidence

Prepare `tricycle_sampling.acquisition_worker` for the complete remaining pool, first staging the validated SAR environment/package locally:

```bash
uv run --package tricycle-sampling --extra replay python -m tricycle_sampling.acquisition_worker \
  --prepare-bundle /absolute/worker-input
```

This generates `run.sh` and reuses `tricycle_sar.sampling_runtime`. Supply the current checkpoint, remaining-candidate CSV, labeled holdout/cross-validation CSV, and the request from `configs/example-agent-acquisition-worker.toml`. Adjust ID/target columns, units, paths, and `model_snapshot_id` to actual inputs. Candidate rows exclude the current selected batch. Reuse `tricycle_sar.get_backend("rxnresid").inference_command`, without another installer or copied tools scripts.

Bohr runs `bash run.sh agent-acquisition-worker.toml`; shared startup initializes the environment and runs the package with its Python after verifying source/backend bindings. This task infers both candidate and validation tables through `bohr.compute.plan/submit`; single native inference uses `sar.infer`. See [example-agent-acquisition-bohr-request.toml](../../../configs/example-agent-acquisition-bohr-request.toml).

Use the same checkpoint for remaining candidates and validation. Predictions must match input IDs one-to-one and include at least `prediction`, `epistemic_variance`, `aleatoric_variance`, and `predictive_variance`. Compute unit-bearing MAE/RMSE from labeled data explicitly marked held-out or cross-validation. The worker only infers/writes outputs: no selection, submission, or checkpoint update. To reflect new labels, finish training/fine-tuning first and supply the actual resulting checkpoint.

Include `outputs/agent-acquisition/` in returned SAR files:

- `candidate-model-signals.csv`: complete remaining rows, predictions, uncertainty, `model_snapshot_id`.
- `validation-predictions.csv` / `model-metrics.json`: predictions, targets, validation role, metrics.
- `worker-report.json`: checkpoint, candidate-space, RxnResid source-tree/output hashes, and candidate-ID coverage.

After downloading/checking both SAR and current calculation workers, merge signals into payload by `candidate_id`. Put model-metrics values in both `observed_metrics` and `context.model_metrics`; record its SHA-256 in `evidence.model_evaluation`. Retain signals/report hashes in candidate-space evidence or `author_notes`.

Require successful SAR worker execution and matching candidate count, ID hash, and output hashes. Missing rows, failed workers, or incomplete reports preclude using partial predictions. Predictions bind the requested checkpoint; if inference ran alongside new-label calculations, it does not include those labels' training effect and cannot be called an updated model snapshot.

## RDKit structural analysis for each round

Do not infer novelty, density, functional-group counts, or reaction edits from a language model's impression of SMILES/images/chemical intuition. After all downloads validate, run repository RDKit analysis on the full unsampled pool and labeled structures. Repair parse failures or incomplete IDs before proceeding; partial features are insufficient.

```bash
uv run --package tricycle-sampling --extra replay python -m tricycle_sampling.structure_analysis \
  --candidates /absolute/path/to/remaining-candidates.csv \
  --labeled /absolute/path/to/all-previously-labeled-reactions.csv \
  --candidate-id-column candidate_id \
  --reaction-smiles-column mapped_reaction_smiles \
  --labeled-id-column candidate_id \
  --labeled-reaction-smiles-column mapped_reaction_smiles \
  --output-dir /absolute/path/to/persistent-round-analysis/structure-analysis
```

Use actual column names. Omit `--labeled` only for a genuinely empty initial history and record that fact. Then `sampled_novelty` is 1.0 for every candidate and does not discriminate ranking or demonstrate an observed coverage gap. Use a new persistent output directory each round, never temporary evidence.

Outputs:

- `structure-features.csv`: original fields plus normalized reaction SMILES; reactant/product atom and complexity descriptors; product stereocenters; mapped bond formation/breaking/order edits where available; nearest-labeled similarity; `sampled_novelty`; unsampled local density.
- `structure-analysis.json`: input/tool hashes, RDKit version, Morgan settings, feature definitions, ID coverage, distributions, and output hashes.

Reaction similarity is the mean of reactant- and product-side Morgan Tanimoto similarities, default radius=2, 2048 bits, chirality enabled. `sampled_novelty = 1 -` maximum similarity to labeled candidates. `pool_local_density` is mean similarity to up to 10 nearest other unsampled candidates. Remove atom maps from canonical SMILES/fingerprints; use complete mappings separately for bond-edit counts. The tool neither normalizes tautomers/charges nor predicts 3D conformations/barriers. Do not overinterpret descriptors as actual reactivity.

Merge features, `candidate-model-signals.csv`, and complete remaining pool one-to-one by `candidate_id`, checking duplicate/missing/extra IDs. Store required features as numeric payload values; never align by row position. V1 must reference tool-generated `sampled_novelty` and, when structural variation exists and matters to the objective, at least one additional discriminating feature such as density, product ring count, or bond edits. Document constant/unmapped/unavailable features and skip them instead of inventing values.

Actually inspect report summaries/features and representative IDs/normalized SMILES in high/low novelty/density regions. Group validation predictions by alignable descriptors, structure clusters, or predefined substructures; inspect group MAE/RMSE, residual bias, and support. Use training/selection-validation only, never final test data to tune acquisition. Where structures/IDs align, compare error hotspots with coverage gaps/distribution shifts. State insufficient sample support instead of making strong regional claims.

If needed signals are absent, first generate candidate features using RDKit substructure matching or another deterministic chemical analysis. Record complete SMARTS/definitions, tool/version, input/output hashes, and checked candidate/structure examples before adding numeric fields. Unencoded subjective claims such as “more reactive” or “more crowded” cannot become scores.

`metadata.agent_acquisition.author_notes` records tool/version/hash, structure/model evidence hashes, major distributions/gaps, grouped errors/support, supporting IDs/structures, chosen/discarded signals, term rationale, cost limitations, and uncertainty. Store absolute feature/report paths and SHA-256 under `evidence.structure_analysis`. `exploration.round` writes formula/context/evidence/notes into hashed `acquisition-function.toml` and trace; preserve referenced raw files for review.

## Pre-decision checks and cost definition

Before `exploration.round`, verify:

- Previous calculations ended. `bohr-batch-status` must show every worker in `status.tsv` as `SUCCEEDED` or `FAILED`, with no RUNNING/PENDING/QUERY_FAILED. Then use `bohr-batch-download` for every worker. Every local `download-results.tsv` entry must be `DOWNLOADED`, with all files listed by corresponding `download-manifest.txt` present. Put report absolute path/SHA-256 in `metadata.agent_acquisition.evidence.downloaded_results`, and status path/SHA-256 in `evidence.calculation_status`. Local validation rejects missing reports, hash mismatch, running/undownloaded workers, or missing files.
- Complete unsampled pool, previous `candidate-pool.tsv`, `selected-candidates.tsv`, and available labels, with SHA-256 records.
- Every payload has `product_count` (prior possible products for the substrate pair) and `mean_product_atom_count` (their mean atom count). Compute missing values from enumeration outputs; do not silently substitute substrate atom count.
- `sampled_novelty`, `pool_local_density`, and mapped edits come from RDKit artifacts joined by ID. Record Morgan/novelty definitions in `context.sampled_distribution.novelty_definition` and table/report hashes in `evidence.structure_analysis`.
- Current `model_snapshot`, checkpoint hash, validation quality, and evaluation artifact. Prefer snapshot-bound holdout/cross-validation metrics over training error as generalization evidence.
- At least one identical metric appears in top-level `observed_metrics` and `metadata.agent_acquisition.context.model_metrics`; runtime validates agreement.
- `context.sampled_distribution` records sampled count, key descriptor distributions, gaps/sparse regions. Explicitly record empty history; no history does not mean covered space. Even an empty history requires local completed-calculation/status/download evidence.
- `context.model_metrics` records actual values, directions, and units. Explain any metric used by the expression in rationale.

For fitted cost relationships, retain training-data artifact hashes and model form. Without calibration, use a transparent proxy and state that it is neither actual Bohr charges nor runtime. V1 requires `cost_expression` using both `product_count` and `mean_product_atom_count`; their product is a linear workload proxy. Other explicit relationships require model/history support and explanation.

## Author and execute

1. Complete prior status/download checks. If workers run or results are missing, pause this decision and finish the Bohr lifecycle first.
2. Update labels/history/model evaluation from downloads. Run/review RDKit outputs for sampled/unsampled distributions and grouped validation residuals, using support counts to identify defensible hotspots/gaps.
3. Evaluate cost for all remaining candidates and inspect total, median, 90th percentile, and maximum; sampling recomputes/persists these statistics.
4. Author `expression` from validation/support, prediction/uncertainty, structure/coverage, and cost evidence. V1 must use candidate `sampled_novelty`, `context.sampled_distribution`, `context.model_metrics`, and `estimated_cost`, plus additional relevant discriminating structure where available. Explain relative-ranking effects rather than only multiplying by a global constant. Adapt to observed evidence/benefit; autonomous MAE permits justified retain without forced formula changes or v1 feature constraints.
5. Put formula, concise rationale, analysis summary, context, and evidence hashes in `metadata.agent_acquisition`, then run:

```bash
uv run --package tricycle-agent tricycle-agent run \
  exploration.round configs/example-agent-authored-exploration-round-request.toml \
  --run-id adaptive-round-001
```

This only selects/records locally. Execute selections through a separate Bohr compute/batch-submit request; wait for completion/download before the next agent decision.

## Expression format

Use `tricycle.safe-numeric-expression/v1`, not executable Python files. Allowed inputs are numeric payload fields, `candidate.field`, `candidate["field"]`, nested `context`, and `estimated_cost`. Allowed operators are `+ - * / // % **`, comparisons/conditionals, and pure functions `abs`, `exp`, `log`, `log1p`, `max`, `min`, `sqrt`. Outputs must be finite; rank descending with ascending `candidate_id` ties.

Example only, to reassess against each round's evidence:

```text
(epistemic_variance * sampled_novelty
    + context.model_metrics.validation_mae
      * (1 - pool_local_density)
      * context.sampled_distribution.coverage_gap) / estimated_cost
```

This is a generic v1 fragment. Autonomous MAE and staged-cost requests use their generated round schema instead:

```toml
strategy = "agent-authored-acquisition"

[metadata.agent_acquisition]
schema = "tricycle.agent-acquisition/v1"
function_id = "adaptive-round-0001"
version = "1.0.0"
authored_by = "agent-authored-acquisition"
expression = "(epistemic_variance * sampled_novelty + context.model_metrics.validation_mae * (1 - pool_local_density) * context.sampled_distribution.coverage_gap) / estimated_cost"
cost_expression = "product_count * mean_product_atom_count"
rationale = "Example: prioritize validation error, novelty, and local sparsity, normalized by expected cost. Rewrite this explanation using the actual round evidence."
author_notes = "Record structure-tool versions and hashes, distributions, grouped validation errors, representative candidate IDs, signal choices, and cost-proxy limitations."

[metadata.agent_acquisition.context.sampled_distribution]
sampled_count = 12
coverage_gap = 0.35
novelty_definition = "1-minus-max-mean-reactant-product-Morgan-Tanimoto-radius2-2048-chiral-v1"
structure_analysis_source = "sha256:<structure-analysis.json SHA-256>"

[metadata.agent_acquisition.context.model_metrics]
validation_mae = 0.21

[metadata.agent_acquisition.evidence]
sampled_set = "sha256:<selected-data-hash>"
candidate_space = "sha256:<candidate-pool-hash>"
model_evaluation = "sha256:<evaluation-artifact-hash>"

[metadata.agent_acquisition.evidence.structure_analysis]
feature_table_path = "/absolute/path/to/structure-features.csv"
feature_table_sha256 = "<structure-features.csv SHA-256>"
analysis_report_path = "/absolute/path/to/structure-analysis.json"
analysis_report_sha256 = "<structure-analysis.json SHA-256>"

[metadata.agent_acquisition.evidence.calculation_status]
manifest_path = "/absolute/path/to/run/status.tsv"
sha256 = "<status.tsv SHA-256>"

[metadata.agent_acquisition.evidence.downloaded_results]
manifest_path = "/absolute/path/to/run/download-results.tsv"
sha256 = "<download-results.tsv SHA-256>"
```

Actual requests retain original metrics, structure/error summaries, cost basis, tool artifact paths/hashes, and analysis. Do not copy example values/rationale as evidence.

## Reproducibility and constraints

- `exploration.round` requires `model_snapshot.snapshot_id`. Complete payloads, observed metrics, seed, expressions, context, costs, and evidence enter provenance.
- Every decision references downloaded, locally validated batch reports. Sampling has no Bohr submit/status/download adapter and cannot share a run task with the QM worker lifecycle.
- `rounds/NNNN/acquisition-function.toml` stores formula/context/evidence/costs/function hash. `model-scores.tsv`, `selection-decisions.tsv`, and `selected-candidates.tsv` preserve scores/per-candidate costs. Manifest and trace cover the function file.
- The current sampling safe interpreter evaluates formulas. Imports, file/network access, arbitrary calls, randomness, system time, and external state are forbidden. Invalid fields, nonpositive costs, or nonfinite scores fail the whole round.
- Every candidate supplies `[0, 1]` `sampled_novelty`. V1 must actually reference it, metrics, and sampled distribution; autonomous MAE retains evidence requirements without forcing those signals.
- Changed later-round functions use a new `function_id` or version. Never overwrite completed rounds. Recovery requires matching request hash and manifest-validated `resume = true`.

## Historical replay worker

New replay trajectories use `packages/tricycle-sampling/src/tricycle_sampling/replay/worker.py`. Main training uses all revealed labels. The independent auxiliary model trains from scratch while excluding fixed `validation_pair_ids`; only absent explicit panels use the worker's deterministic one-fifth holdout. `model-metrics.json` identifies auxiliary checkpoint hash, training/validation pair lists, and metric ownership. Do not call these main-model holdout errors.

Remaining-candidate signals come from the main model, summing uncertainty over all prior enumerated products per substrate pair. Local `packages/tricycle-sampling/src/tricycle_sampling/replay/campaign.py` validates complete coverage, executes RDKit, generates grouped validation errors/history, and produces a decision directory containing only sampling evidence. The agent returns formulas/analysis without accessing GT evaluation files or submitting Bohr. See [intelligent replay](../../../docs/intelligent-replay.md).

## Concise decision output

Write `rationale` as 2–4 concise English sentences, preferably at most 120 words, explaining priorities, evidence, cost tradeoffs, and key limitations. Keep `author_notes` within 600 words, carrying candidate IDs, structural examples, grouped errors, coefficient rationale, and evidence references. Detailed raw evidence remains in linked artifacts. These language defaults also apply to `hypothesis`, `expected_effect`, and review/summary text, unless the user explicitly requests another language.

The replay supervisor writes rationale, full formula, cost expression, selected count, and original-evidence links to each round's `decision/ACQUISITION.md` and updates trajectory/experiment `ACQUISITION.md`. No additional model-summary call is needed.

### Replay decision materials and response

The controller runs RDKit over the full pool, validates evidence hashes, unique/unsampled IDs, structural-route coverage, and feature aggregation, then generates `decision-brief.json` in current `tricycle.replay-decision-brief/v2` format. Materials include full-pool feature/cost distributions; low/mid/high representative structures; correlations; ring-count coverage; grouped validation errors/support; worst validation errors/structures; error changes; exploration/high-uncertainty intersections; and the last three formulas. Representatives are not preselection: the final formula still scores every unsampled candidate. Review supplied materials without rerunning analysis or reading complete raw tables.

Follow current `response-schema.json`: basic responses return expression, cost_expression, rationale, author_notes; autonomous MAE also requires decision_action, hypothesis, expected_effect. Brief format does not change the selected acquisition protocol.

Read model/reasoning settings from `decision_agent`. Invocations disable shell/unified_exec/sleep and web search. No tool calls, log reading, provenance transcription, or hash generation/calculation are allowed inside the decision call. After process return, the controller validates the original task and binds hashes, retaining raw output separately from bound response, then validates safe DSL across the full pool and selects deterministically.

Each invocation uses the smaller of `agent_timeout_seconds` and 600 seconds. `max_decision_attempts` defaults to three including the initial attempt. Preserve timeout/invalid-output records and return concrete errors for correction. `decision-input.json` stores prompt, material hashes, preparation duration, and character counts; `agent-exit.json` records invocation duration; `decision-validation.json` records full-pool validation duration. Retain complete raw candidates/evidence. Implementation updates never justify redoing completed selections.
