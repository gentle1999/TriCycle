---
name: exploration-round
description: "Record a strict, replayable exploration round, including candidate-space decisions and the RitS train-or-skip gate."
---

# Exploration Round Provenance

Use to record a complete active-learning/reaction-exploration round: pool, model scores, selection, next-space decision, and RitS train-or-skip decision.

## Inputs

- `exploration_id`, positive integer `round_index`, optional `parent_round_id`.
- Complete `candidates`, `budget`, strategy, and random `seed`.
- `space.space_id`, `space.version`, and explicit `space.definition`.
- Uncertainty strategies require `model_snapshot.snapshot_id` and checkpoint hash.
- For `agent-authored-acquisition`, finish previous-worker status checks and download all results locally, then create the request under [its skill](../agent-authored-acquisition/SKILL.md).
- `range_policy`: `fixed` preserves the space; `explicit` uses requested `next_space`.
- `training_policy`: `never`, `always`, `every_n_rounds`, `new_labels`, `every_n_or_new_labels`, `dataset_changed`, or `manual`.
- Scheduled training requires reproducible `training_spec`, including `seed`, `dataset_manifest_hash`, `code_revision`, and `container_digest`; optional `next_model_snapshot` binds an already-produced checkpoint.

Put prior selections in `history`, training context in `training_context`, and destination in `output_dir`. See the [request contract](../../../packages/tricycle-exploration/README.md). Use canonical fields/strategy IDs only; unknown fields, old aliases, and duplicate top-level training context are not supported.

```bash
uv run --package tricycle-agent tricycle-agent run \
  exploration.round configs/example-exploration-round-request.toml \
  --run-id exploration-round-001
```

This creates local provenance only; it neither submits Bohr nor automatically starts RitS training.

## Outputs

`rounds/NNNN/` contains:

- `space.toml`: definition/hash.
- `candidate-pool.tsv`: complete pool and exclusions.
- `model-scores.tsv`: score provenance, model snapshot, checkpoint hash.
- `acquisition-function.toml`: agent strategy only; formula, context, cost statistics, evidence.
- `selection-decisions.tsv`: ranking, stable tie-breaks, selection, and rationale.
- `range-decision.toml`: next-space policy, input evidence, and outcome.
- `training-decision.toml`: explicit RitS train/finetune/skip decision and rationale.
- `snapshot-transition.toml`: input/next snapshots, training recipe, stable replay key. Skip explicitly retains the input snapshot; training without a produced checkpoint records a deterministic planned snapshot.
- `artifacts/manifest.toml`: SHA-256 manifest.

`trace/events.jsonl` records stages through a hash chain. `training_action = skip` is an explicit decision; absence of a training run does not explain a skip. Training, selection, and range decisions form `round_decision_hash`; together with model snapshot, dataset hash, and `training_spec`, this defines the next-snapshot replay key, verifiable without rerunning QM.

CLI `resume.next_action` remains `exploration.round` for round reuse. Required `rits.train`/`rits.finetune` appears in `resume.follow_up_action` and needs a separate RitS request.

## Constraints

Record policy IDs, versions, parameters, and input hashes instead of unversioned ad hoc range/training code. Full pool/scores are required for replay; top-N alone is insufficient. Agent acquisition runs separately only after calculations terminate and downloads complete. `resume = true` reuses only an existing round with matching request hash and never overwrites artifacts. Write new decisions, explanations, and records in English unless the user explicitly requests another output language.
