# Component and capability contract

This document defines Python component interfaces. The public unit invoked by Codex is a `CapabilityManifest`/capability ID. See [architecture](architecture.md) for composition and execution boundaries.

## Shared lifecycle

Every component provides at least:

- `PluginManifest`: stable `plugin_id`, version, capability list, and dependency information.
- Domain request dataclasses, avoiding unspecified dictionaries across plugin boundaries.
- `plan(...) -> TaskPlan`: pure planning that produces parameters, inputs, and intended operations.
- `execute(...)` or a domain-specific side-effect method using an injected adapter; a scaffold may return only `planned`.
- A capability skill at `.agents/skills/<skill-name>/SKILL.md`, describing when to use it, inputs, outputs, failure handling, and prohibited actions.
- For long workflows or active learning, structured execution events through `ExecutionContext.trace_sink`. Events do not replace artifacts: persist full inputs and decisions with hashes.

## Capability contract

`CapabilityManifest` in `tricycle-core` declares an independently callable capability:

- `capability_id` is the stable Codex invocation ID.
- `component` identifies the domain component without making core depend on it.
- `input_kind` and `outputs` describe TOML request and artifact boundaries.
- `side_effect`, `requires_auth`, `supports_dry_run`, and `resumable` describe safety and recovery policies.

The application's `CapabilityCatalog` explicitly binds capability IDs to component operations. `CapabilityDispatcher` injects adapters and executes them. New skills must not directly import internal component classes.

## TaskPlan

`TaskPlan` is the auditable cross-plugin intermediate representation. It includes at least:

- `task_id`, traceable within a run;
- `plugin_id` and `operation`;
- `inputs`, comprising input artifacts or entity IDs;
- JSON-serializable `parameters`;
- `metadata`, such as source, version, and idempotency key.

## External boundaries

- Bohr: implement `BohrGateway` with at least submit/status/download. Batch lifecycles may additionally implement job groups, termination, and related operations.
- Reaction database: implement `ReactionRepository.upsert` for individual records. Batch workflows implement `ReactionDatabaseAdapter` to create an internal project for one batch, submit calculation logs, invoke database rxn-SMARTS filtering, and export RitS/RxnResid datasets.
- ReactionOpt: implement `ReactionOptAdapter` to translate intermediate configurations into actual ReactionOpt calls.
- ReactionOpt profiles: inject the reaction-level RitS fork pipeline through `RitsTransitionStateSampler`. Reactant conformers, product conformers, and RitS TS generation are independent branches combined only in the final profile. The TS branch must use candidate generation, stereo filtering, Kabsch RMSD clustering, and g-xTB screening from `run_ts_pipeline.py`; raw `sample_transition_state.py` outputs are not final TS collections. RitS consumes mapped reaction connectivity directly and must not fall back to the old product-backward initial guess. Reaction SMILES must include every hydrogen as a separate mapped `[H:...]` atom. All atoms need unique positive maps, with identical map sets and elements on the reactant and product sides.
- Model training/fine-tuning/inference: RxnResid (`sar.*`) and RitS reuse `BohrModelJobRequest`, `BohrModelPlugin`, and `BohrJobExecutor` from `tricycle-bohr`, producing `train`, `finetune`, and `infer` operations. Domain components own input preparation, source/asset bindings, and commands while sharing the submission boundary.

Protocols do not prescribe a transport. Production adapters include Bohr CLI, Reaction DB MCP/HTTP, and internal SSH/Docker. Domain requests and default dry runs do not instantiate these clients.

## Run traces and exploration rounds

`TraceStore` appends run events to `trace/events.jsonl`, with sequential numbers, previous-event hashes, and each event's own hash. The CLI records lifecycle events for run/resume, planning, and capability completion/failure. Domain events should reference specific artifact-relative paths and SHA-256 hashes instead of merely saying that an operation completed.

Active-learning `exploration.round` must persist the complete candidate pool, model scores, and input model snapshot in the round directory. It also records exclusion reasons, stable tie-breaking, sampling strategy/version/seed, scope policy, and RitS train/skip evidence. `snapshot-transition.toml` derives a replay key from `round_decision_hash`, dataset hash, training recipe, and input snapshot. A skip transition must preserve the input snapshot. Before training completes, use a deterministic planned snapshot, then bind `next_model_snapshot` to the actual returned checkpoint. QM calculations are outside this replay function, but their candidate inputs and dataset artifact hashes must already exist.

`agent-authored-acquisition` is a local sampling strategy without external side effects. An agent authors a safe numerical expression per round; `tricycle-sampling` validates the limited syntax, evaluates deterministically, orders by cost, and hashes the formula. Each decision must also reference paths and SHA-256 hashes for local `status.tsv` and `download-results.tsv`. All workers must be terminal, and every file in the download manifest must exist. This gate separates selection from computation submission/execution: sampling cannot submit candidates or operate the Bohr lifecycle within the same calculation task. Candidate fields are validated against the chosen acquisition protocol; v1 and MAE v2 have different signal constraints. See the [sampling interface](../packages/tricycle-sampling/README.md#agent-authored-acquisition-and-custom-strategies).

## Bohr workflow boundaries

`tricycle-bohr` provides single-job infrastructure: `BohrJobSpec`/`BohrGateway` define the platform boundary, `BohrJobExecutor` unifies planned submission, and `BohrModelJobRequest`/`BohrModelPlugin` unify model artifacts, train/finetune/infer operations, and submission. RxnResid/RitS own domain requests, input preparation, asset validation, and commands without duplicating Bohr submission.

`tricycle-bohrsubmit` provides typed Python APIs for local staging, Gaussian/DPA-Berny inputs, TSV lifecycle records, and rescue policies. Cloud workers still generate small Bash launchers because Gaussian, the DPA server, and user-supplied ORCA commands are process boundaries inside images. `BohrCliGateway` centralizes the `bohr` CLI, retries, and submission deduplication.

TriCycle-owned declarative requests and run metadata use TOML. Task-specific configuration is an input artifact whose format belongs to the task; for example, RitS Hydra YAML may be uploaded unchanged. Environment variables remain separate. Bohr CLI authentication and worker Bash runtime variables come from the process environment or `.env`, rather than being stored in TOML.
