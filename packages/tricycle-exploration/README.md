# tricycle-exploration

`tricycle-exploration` records reproducible local provenance for one round of active learning or reaction-space exploration. It does not submit Bohr jobs or automatically launch RitS; an explicit `training_policy` alone determines RitS training.

## Round records

`ExplorationPlugin.execute_round()` runs a registered sampling strategy over the complete candidate pool and saves:

- The versioned input space and its hash.
- The complete candidate pool, model scores, model snapshot, exclusions, rankings, and stable tie-breaks.
- The `fixed`/`explicit` range policy and its input metrics.
- The RitS `train`, `finetune`, or `skip` decision, thresholds, and rationale.
- `snapshot-transition.toml`, linking the input snapshot to the next round's snapshot.

When training is skipped, the next snapshot exactly equals the input snapshot. When training is scheduled but no checkpoint exists yet, the module derives a deterministic planned snapshot and replay key from the input snapshot, `round_decision_hash`, dataset hash, and complete `training_spec`. Once training finishes, `next_model_snapshot` can bind the actual checkpoint without changing the replay key. The training recipe requires at least `seed`, `dataset_manifest_hash`, `code_revision`, and `container_digest`. The package does not repeat QM computations; candidate inputs and dataset hashes serve as boundary evidence.

## Request contract

Requests use the canonical fields `exploration_id`, `output_dir`, `parent_round_id`, and `next_model_snapshot`. Candidates use `candidate_id`; additional space properties belong explicitly in `space.definition`; model snapshots use `snapshot_id`, `checkpoint_hash`, and `source_run_id`.

Selection history belongs in `history.seen_candidate_ids` / `history.exclude_seen`. Training context belongs in `training_context` (`last_training_round`, `new_label_count`, `dataset_before_hash`, `dataset_after_hash`, `previous_checkpoint`, and `action`), while the training recipe belongs separately in top-level `training_spec`. `to_dict()` emits the same reparsable request structure; `round_id` is a derived object property. Unknown fields and duplicate legacy entry points are errors.

## Run artifacts

```text
<run>/
├── trace/events.jsonl                  # Run-wide hash-chain event ledger
├── request.toml                        # Original request
└── rounds/0001/
    ├── space.toml
    ├── candidate-pool.tsv
    ├── model-scores.tsv
    ├── selection-decisions.tsv
    ├── selected-candidates.tsv
    ├── range-decision.toml
    ├── training-decision.toml
    ├── snapshot-transition.toml
    ├── round.toml
    └── artifacts/manifest.toml
```

`resume = true` reuses only a round with the same request hash and complete artifacts; it never overwrites existing files. CLI state keeps `resume.next_action` as `exploration.round` for safe round reuse/verification. If the gate requests training, `resume.follow_up_action` explicitly records `rits.train` or `rits.finetune`; execution still requires a separate RitS request.

## Example

```bash
uv run --package tricycle-agent tricycle-agent run \
  exploration.round configs/example-exploration-round-request.toml \
  --run-id exploration-round-001
```

The pure functions `decide_range()`, `decide_training()`, and `resolve_snapshot_transition()` allow tests to verify next-snapshot semantics without writing files.
