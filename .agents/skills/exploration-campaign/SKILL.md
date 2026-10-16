---
name: exploration-campaign
description: "Supervise a persistent multi-round Bohr exploration, hand off analysis to an agent, and recover from interruptions."
---

# Persistent Exploration and Agent Handoffs

Use English for execution plans, delegated tasks, decisions, analysis, and new records even when the user writes in Chinese, unless the user explicitly requests another language. Propagate any explicit language exception to delegated agents. Preserve existing evidence in its original form.

## Matched sampling experiments

For a new matched comparison use `python -m tricycle_sampling.replay.suite prepare --config configs/dpa-unified-sampling-suite.toml`. Preparation is local and reads no credentials or network. Check suite/preflight bindings for shared data, initial sets, auxiliary panels, training, and evaluation. After experiment execution is authorized, advance with `watch --run-dir <suite> --live --allow-submit`. `service-file --run-dir <suite> --allow-submit` generates an independent launchd file without installing it.

Read comparison methods from `sampling_strategies`. Baselines use the shared numerical adapter; intelligent strategies follow their selected protocol. Check `suite.json`, `preflight.json`, and each `experiment.json` for initial sets, seed/round counts, batch sizes, epochs, evaluation, and concurrency limits. Supported preparation shapes constrain this suite, not every campaign's budget.

`autonomous-core-hours-mae-v1` compares GT MAE at fixed cumulative proxy core-hour budgets using only the latest completed checkpoint within each budget. Do not extrapolate or stop/switch based on GT performance. Estimate costs from revealed history only, without unknown actual costs in acquisition. Evaluation nodes are not hard spending guarantees. Fixed proxy-panel feedback differs from main-model full-pool GT error. Read seed launch gates from `rollout.pilot_evaluations`; do not eliminate strategies by evaluation error.

All jobs share a persistent Bohr group. Isolate failures per seed and preserve inflight state, accepted decisions, and receipts. Never resubmit blindly. Source/config/data/worker hash mismatches reject continuation by default; do not refresh historical hashes to migrate experiments.

For replay supervision, maintain state once per seed trajectory: resume from
`seed-XX/state.json` and inspect `seed-XX/state-events.json` for compact operational
transitions. Do not copy controller state or its journal into individual round
submissions. Ordinary polls update only current state; old `history/*.json`
snapshots remain historical evidence. A journal event ahead of the current state
revision may be an interrupted write, not a committed transition. Keep per-round
submission receipts, decision evidence, and sampling history unchanged.

For user-authorized blocked-trajectory email, follow the
[alert guide](../../../docs/trajectory-alerts.md). Configure SMTP host, port, TLS,
credentials, sender, and recipients through environment variables, and explicitly
enable `TRICYCLE_TRAJECTORY_ALERTS_ENABLED`. The reporting subagent works in English
but authors equivalent English and Chinese email fields as the requested exception.
It must not send mail, choose recipients, change scientific work, or infer a proven
root cause from an error alone. The controller validates its response and delegates
delivery to the shared SMTP service. Keep outbox evidence/drafts/receipts under
`.tricycle/alerts`, not in every round. Inspect exhausted deliveries and use an
explicit `alerts scan --live --retry-failed` after repair; do not delete deduplication
receipts or resume scientific work merely to resend an alert. An independent alert
observer can monitor existing blocked states without changing frozen controller
bindings. Dry-run inspection never calls the agent or sends email.

If the user explicitly authorizes continuation after storage adaptation, first compare source assets matching original hashes and verify byte-equivalent inputs, then create a separate audit receipt under the package README's `storage-compatibility.json` contract. Only reviewed storage-compatible changes are allowed; retain original configuration/hashes and scientific protocol/budgets.

Optional intelligent variants come from `replay.suite.AGENT_VARIANTS`; current configuration determines comparisons. Job counts, audits, and figure layout must match it. Seed-watch supervises the selected strategy; formula recovery follows each tool's protocol scope. Intelligent DSL audits support intelligent references/expressions and do not replace complete numerical acceptance of deterministic baselines. Also check platform audit pagination limits. See the [experiment/recovery guide](../../../docs/mae-intelligent-sampling.md), [methods](../../../docs/sampling-baselines.md), and [package interfaces](../../../packages/tricycle-sampling/README.md).

## QM campaign startup and supervision

This application orchestration capability reuses domain plugins. Input is `configs/example-exploration-campaign.toml`; state is `campaign.toml` in a dedicated directory. On startup/handoff, read state, active task, and prior artifacts instead of relying on chat memory.

```bash
uv run --package tricycle-agent tricycle-agent campaign start campaign-request.toml
uv run --package tricycle-agent tricycle-agent campaign status --run-dir /absolute/campaign
uv run --package tricycle-agent tricycle-agent campaign tick --run-dir /absolute/campaign
uv run --package tricycle-agent tricycle-agent campaign watch --run-dir /absolute/campaign --live
```

`start` registers an already-submitted batch without contacting services. Default `tick/watch` is read-only dry-run: no credentials, agent invocation, or stage advancement. `--live` enables query/download/configured agent commands. Explicit user authorization for later batches permits `--live --allow-submit`, within `max_new_batches` and `max_selected_candidates`. These exclude the initial batch and are not monetary budgets.

`initial_selected_ids` should include the already-sampled set. Immutable `context_paths` files provide full structures, labels, and model information. Do not edit an active request to expand budget.

Only one process advances a campaign; exit releases its lock. `watch` polls and immediately continues after stage changes, without model calls just to wait. `--max-ticks N` bounds this supervision session; files retain state. Production restarts use existing supervisors/system services; TriCycle does not install them automatically.

## Delegated-agent protocol

When `agent_command = []`, the supervisor reads the task at `state.agent_task_path` and delegates it to one subagent. Assign each task to exactly one executor. The agent atomically writes the specified `response_path`. On handoff, check existing responses/selections first; do not regenerate completed decisions.

Alternatively inject `agent_command = ["/absolute/path/to/agent-wrapper", "argument"]`. The program appends absolute `task.toml` and `response.toml` paths and executes argv without a shell. The wrapper waits for completion, exits zero, and writes the response. Configure SDK/account access explicitly in the wrapper; the repository assumes no platform-specific subagent API. Logs/PID remain in the stage directory. Interrupted invocations are not blindly restarted.

### Analyzing stage

Read task batch/evidence/context and prior rounds. Follow [agent-authored-acquisition](../agent-authored-acquisition/SKILL.md): verify all worker downloads, collect valid labels/failures, finish required model updates/inference and per-round RDKit analysis, inspect grouped errors, and generate a complete `exploration.round` TOML request. Independent model jobs must remain within existing authorization and preserve runs/artifacts. Planned checkpoints are not trained models. Do not submit new QM candidates.

`exploration_id`/`round_index` must match the task and `budget` cannot exceed remaining candidate allowance. `metadata.agent_acquisition.evidence` calculation_status/downloaded_results must exactly match `evidence.toml`. Use complete pools/history from context/prior rounds, not only top-N.

```toml
task_sha256 = "<SHA-256 of task.toml>"
request_path = "/absolute/persistent/exploration-round-request.toml"
request_sha256 = "<SHA-256 of request file>"
```

If candidates are exhausted or evidence supports a reached accuracy target, return `action = "stop"`, nonempty `reason`, and `task_sha256`. Preserve analysis and original supporting reports in the stage directory, not only chat text.

### Preparing stage

Validate task `selection_path`/`selection_sha256`. Use existing preparation capabilities to convert **all selected candidates** into a new independent QM batch, preserving mappings to actual calculation inputs. Do not submit. Create only nonempty workers; the current acquisition evidence contract rejects SKIPPED_EMPTY.

```toml
task_sha256 = "<SHA-256 of task.toml>"
batch_run_dir = "/absolute/path/to/new-prepared-batch"
plan_sha256 = "<SHA-256 of submit-plan.tsv>"
selection_sha256 = "<selection_sha256 from task>"
candidate_ids = ["<selected candidate ID>"]
```

The scheduler checks IDs, selection hash, and prepared files, then freezes worker-input hashes before submission. The preparer owns chemical mapping into Gaussian/DPA tasks; declaring IDs does not replace that validation.

## Pause, recovery, and failures

### Live replay record containers

New replay preparations write directly to one SQLite namespace per seed during
execution. Configure durable local `TRICYCLE_RECORDS_ROOT`; never use a remote
mount for live WAL writes. Read logical records through campaign interfaces or
`replay.runtime_records read/list/verify`. State and its transition journal are
logical keys, not ordinary files. External handoffs are temporary local files
whose locations are recorded for recovery.

Follow [live replay records](../../../docs/compact-replay-records.md) for consistent
snapshots, explicit migration, and `recover-agent`. Migration requires paused
supervisors and readers, exact-byte preservation, and an audited storage-only
source amendment when bindings change. Preserve original failure records and
scientific bindings. New agents use the same English instructions and numerical
validation. Interrupted output imports never authorize duplicate model calls or
remote submissions. Native containers need no completion-time consolidation;
`record_store watch` finalization only verifies them. Historical sealed records
retain their explicit restore path. These interfaces do not replace the QM CLI.

```bash
uv run --package tricycle-agent tricycle-agent campaign pause --run-dir /absolute/campaign
uv run --package tricycle-agent tricycle-agent campaign resume --run-dir /absolute/campaign
uv run --package tricycle-agent tricycle-agent campaign watch --run-dir /absolute/campaign --live --allow-submit
```

Pause takes effect after the current operation and does not terminate remote jobs. Resume clears pause/error counters without directly performing external actions. Status/download errors use backoff and become blocked at max_failures; repair the cause before resuming.

Original Bohr download reports live at `downloads/download-results.tsv`. The orchestrator atomically copies them to batch root for acquisition validation and records source path/hash in round `download-source.toml`. Agents write responses to temporary files then rename, preventing partial TOML reads.

After interrupted agent commands, inspect `agent-process.toml`, logs, and artifacts to establish process state and recover responses. If restart is necessary, confirm the old process stopped, explicitly remove that stage's `agent-invocation.toml`, then resume. Do not delete accepted responses or frozen selection requests to redo decisions.

Submission errors/process exits preserve `state.inflight`. Check Bohr and each worker `submit.out`, then complete truthful `submit-results.tsv`. Monitoring begins automatically only when all planned workers have receipts. Never clear inflight or resubmit when submission outcome is uncertain. For genuinely missing partial-batch submissions, complete remote reconciliation first and use the existing Bohr recovery flow; supervision never guesses remote IDs.

Replay submission timeouts retain the round's `submission-inflight.json` and automatically reconcile through the Bohr adapter. Reuse an exact remote match; permit resubmission only after two complete absence checks at least 60 seconds apart, with at most three restarts per round. Incomplete or ambiguous remote listings cannot prove absence. Preserve `submission-reconciliation/` and `submission-timeouts/`. Agent invocation timeouts use separate proposal/review restart counters (three restarts each) and retain failed calls under `decision/timeout-attempts/`; they do not consume scientific correction attempts. A timed-out review restarts from its validated proposal. Exhaustion retains evidence for explicitly authorized recovery.

After budget exhaustion, monitor/download the last submitted batch before marking complete, without another acquisition call. Audit with `history/*.toml`, round tasks/responses/evidence, selection traces, and results.

## Historical-label replay

When explicitly selected, new replay experiments use `python -m tricycle_sampling.replay.suite prepare` and `configs/dpa-unified-sampling-suite.toml`. Existing single experiments recover through `python -m tricycle_sampling.replay.campaign` using their run-local configuration. See [intelligent replay](../../../docs/intelligent-replay.md). Historical experiment operation records are local research archives, not defaults for new campaigns. Replay does not enter the QM preparing stage above.

Create new trajectories from validated baseline initial sets and **retrain from those sets**, without baseline checkpoints or later baseline selections. Main/independent validation models use requested epochs. Reveal historical labels only for selected pairs round by round; latest GT is exclusively final evaluation. After SAR download/RDKit analysis, the agent writes a formula; `tricycle_sampling.replay.selection.select_round` selects deterministically and records audit evidence before training the trajectory's next model. Generic QM still uses `exploration.round`; replay does not depend back on exploration/tools.

Dry-run does not connect. Live startup needs `--live --allow-submit` within prepared seed/round budgets. All replay Bohr tasks, including five trajectories and appended rounds, reuse the CLI group ID in root `job-group.json`, created once under a shared file lock before first submission. Never create groups per seed/round. Cancellation/resubmission requires explicit authorization and preserved old receipts; changing a local group field does not migrate submitted jobs.

### Decision failures and bounded correction

Each seed runs an independent decision process without a shared serial model-execution lock. Read model/reasoning from `decision_agent` and timeout from `agent_timeout_seconds`. Format, task-hash, formula, numerical, process, and timeout failures preserve rejected-attempts and feed concrete errors back to the model. `max_decision_attempts` bounds retries; exhaustion blocks only that seed. Accept formulas only after full-pool prevalidation, without changing completed selections. Uncertain submission still requires receipt reconciliation.

Replay Bohr `max_reschedule_times` defaults to 2 and accepts nonnegative integers in `[bohr]`. The adapter passes CLI `--max_reschedule_times`, and each `job-spec.json` records it. Node reclamation/physical-host failures may trigger up to two whole-round platform reruns; this does not imply mid-round checkpoint resume. Submission API retries remain disabled (`submit_retry_mode = none`), and existing jobs are unchanged. Adapter defaults are authoritative; configuration affects future submissions only.

Validation failures enter automatic agent repair with the exact error, last accepted round formula, and separately labeled rejected proposal. The agent makes the minimal necessary correction; the controller revalidates and continues automatically. `retain` refers only to the accepted previous round, never a failed proposal. After exhaustion, user-authorized repairs use the package README's `decision-controller-amendment.json` and task-bound `recovery-authorization.json` contracts. Preserve original hashes and rejection counters, allow only the explicitly bounded additional attempts, and disclose the intervention in comparisons. Do not use the storage-compatibility receipt for decision-protocol changes.

### Decision delivery and recovery

Controllers generate `decision-brief.json` and `response-schema.json`; agents review supplied evidence and return formulas/analysis. See [replay decision materials and response](../agent-authored-acquisition/SKILL.md#replay-decision-materials-and-response) for fields, task bindings, and invocation boundaries. Supervisors check complete raw evidence, response bindings, and full-pool validation before advancing, preserving accepted decisions/selections. Unknown `decision_protocol` fields or contradictory objectives are rejected; recovery must not silently disable feedback/review.

Report model 401/invalid API key/expired token failures as authentication issues, not formula-correction retries. Before recovery compare failed-call startup with local login refresh time. Preserve failures and resume only unfinished selection decisions; do not retrain completed rounds or resubmit existing jobs.

Independent `tools.balance_alert` provides balance warnings without consuming model quota or changing experiment budgets. Enable only after configuring a real balance adapter, threshold, and SMTP recipients; see [balance alerts](../../../docs/balance-alert.md). Authentication failure and exhausted balance are distinct; 401 alone does not prove low balance.

### Single-space comparison suites

`python -m tricycle_sampling.replay.suite prepare --config configs/dpa-unified-sampling-suite.toml` prepares five literature baselines and intelligent sampling locally. `suite.json` records root/config hash/initial sets; `preflight.json` records all first-round input preparation and initial-set consistency. The old hardcoded one-off DPA preparation script is removed. After startup authorization, use suite `watch --live --allow-submit` or `service-file`.

Deterministic `sampling_strategy` choices come from `replay.baselines.STRATEGIES`, containing current literature baselines only. New preparations record current `sampling_experiment` schema; missing/unsupported old schemas do not trigger fallback algorithms. Preserve old records and prepare new runs for recomputation. Other generic `candidate.select` strategies are unaffected. Baselines run numerically without model calls and are not labeled agent-authored. Preserve full `baseline-ranking.json`, `baseline-selection-request.json`, and preparation-frozen implementation hash.

With `bohr.shared_group_dir`, all experiments create/reuse one group under shared lock with durable receipt. `rollout.pilot_evaluations=3` holds seed01–04 until that strategy's seed00 third evaluation, then advances automatically. Do not skip remaining seeds because error is high or clear uncertain-submission markers to resubmit.

When the user explicitly requests maximal seed parallelism, `rollout.pilot_evaluations = 0` lets all five trajectories advance independently. Before changing active scheduling, pause local supervision, await released seed-operation locks, archive original config/hash, record authorization, and update config hash while preserving receipts/state. This changes launch order only, not seed/round/sample budgets; rounds within a trajectory remain sequential.

For authorized additional static strategies, create separate trajectories per space/strategy without editing active frozen configurations. `rits-zero-shot-da` and `autode` aleatoric-uncertainty/predictive-uncertainty reuse DPA's deterministic implementation: sum uncertainties across unlabeled prior product routes, rank descending, break ties by pair ID. Per seed, match first-round inputs, initial pairs, fixed validation panel, data/hashes, version, epochs, and core-hour nodes to existing same-space controls. Never reuse another strategy's checkpoint or GT ranking. Update sampling plans, supervisor lists, experiment/trajectory/job totals, and status reports together.

### Validation MAE monitoring

`python -m tricycle_sampling.replay.validation_monitor --run-dir <root-containing-sampling-plan.json>` reads only completed rounds validated for worker-output hashes, source version, fixed panel, and no training leakage. It produces `VALIDATION_MAE_REPORT.md`, latest/history CSV, and source-hashed JSON. Supervisors may call `update(root)` and include the report in their overview without changing training/decisions or preserving historical controller source.

Distinguish auxiliary holdout MAE on the fixed initial panel from main-model full-GT MAE. The latter is read-only evaluation, excluded from acquisition. Show per-seed last completed round, stage, MAE, change from prior completed round, sample count, and cumulative core-hours. Missing results remain missing, not zero. Strategy means of each seed's latest values are monitoring summaries, not fair rankings across different rounds/costs.

For requested cost/GT-error figures use `analysis.sampling.replay_sampling_panels`. Freeze completed checkpoints from a root with sampling-plan.json or suite.json, then render in the report environment. Layout actual spaces; upper panels show GT MAE and lower panels paired same-seed/same-cost method-minus-reference differences. New comparisons use predictive uncertainty as reference; historical snapshots retain random when present.

Static lines are dashed, intelligent solid; bands show sample SD. Record available seed count at every incomplete-trajectory point and mark incomplete coverage under the plotting interface; no band for n=1. Use latest completed budget-eligible checkpoints within observed cost coverage, without extrapolation. Lower panels aggregate paired-seed differences only. Report fixed auxiliary-panel MAE separately from full-pool GT MAE.

For an authorized English-versus-Chinese policy comparison, use the offline `analysis.sampling.language_comparison` observer with a hash-bound comparison plan; see the [analysis interface](../../../analysis/README.md). Preserve historical records and freeze reference evidence before waiting. Compare paired seeds at the original budgets only after English trajectories complete or block, report partial coverage, and disclose other policy/infrastructure differences. Keep these GT comparisons outside acquisition evidence. The observer never submits or retries experimental work.

Apply [run record scope](../../../docs/run-records.md): new comparison plans, snapshots, figures, and reports belong under `reports/`. Keep execution decisions, rejected attempts, receipts, and necessary recovery state in the owning run. Independent source backups, code diffs, and repository-wide repair audits belong under `artifacts/audits/`; a run stores only the intervention receipt and evidence path/hash. Preserve already-bound historical evidence paths.

### Shared assets and compact inputs

New replay `asset_store` points at project-level storage, defaulting to `artifacts/task-assets` under the working directory. Validate `asset-references.json`, per-round `input-assets.json`, and bindings; submit through current controllers/adapters. New runs retain no `assets/`, `private/`, per-round `input/`, or corresponding symlinks.

`input-assets.json` binds complete round delivery. Local reads use `input_file`; adapters materialize complete upload inputs temporarily and clean them automatically. Recovery/audits check shared assets and checkpoint chains. Stop on missing/changed assets instead of changing hashes or borrowing trajectory models. Downloads/results are shared by hash; moving/archiving runs must preserve referenced stores. Do not refresh historical bindings; after structural changes prepare new runs under the same scientific protocol.

Sampling downloads select only worker `outputs/round/`. Training staging/upload assets are not results. Workers publish new weights, predictions, metrics, and logs under the result manifest. Reused previous weights return only hash-bound `output_references`; the controller restores local references from that trajectory's inputs without downloading the model again. Do not widen backward_files to the entire workspace or add refit input weights/preprocessed data to results.
