# Live replay record containers

New replay preparations write trajectory records directly to one transactional
SQLite namespace per seed. Logical keys retain familiar names such as
`state.json` and `round-0003/decision/response.json`; they are not physical files.
State, decisions, full acquisition history, rejected attempts, timeout evidence,
submission receipts, and agent log chunks enter the container during execution.
Controllers, audits, status commands, and portable scientific exports read it
directly. Completion does not trigger a file-packing step for native containers.

## Storage and ownership

The foundation modules `tricycle_core.record_ledger` and `record_io` provide
transactions and explicit logical I/O. Sampling owns initialization, migration,
agent handoffs, and recovery in `replay.runtime_records` and `agent_records`.
No global file APIs are patched. Existing expanded records remain readable;
historical sealed `tricycle.replay-records/v1` containers retain their reader.

Set `TRICYCLE_RECORDS_ROOT` to a durable **local** directory before preparation.
The default is `.tricycle/record-ledgers` under the working directory. Each
seed's `records.sqlite` points to its uniquely named database there. These
host-managed databases contain task-owned evidence: they are not disposable
caches. Retain both the run and its referenced database/asset stores. A SQLite
WAL and shared-memory file may exist beside the database, independently of the
number of rounds or record keys. Coordination locks stay on the filesystem.

Do not place the live database on rclone, NFS, or SMB. Recognized network/FUSE
filesystems are rejected during creation. Use the SQLite backup operation to
publish a consistent, closed snapshot, then transfer that one file to an archive.
Never synchronize the database, WAL, and shared-memory files independently.
A snapshot alone does not include referenced model/data assets.

Shared inputs, weights, and results remain content-addressed references; they
are not repeatedly embedded. External commands still need a small, explicitly
owned local handoff: an agent schema/output pair, an upload manifest, or worker
download files. Handoff locations are recorded inside the container. Validated
output commits before successful handoffs are cleaned up. Interrupted handoffs
remain for recovery. Environment/cache trees and credentials are rejected as
record keys. Other TriCycle task families retain their own storage interfaces.

## Durability and audit

Each record mutation and its audit event commit in one SQLite transaction with
full synchronous durability. Current state and its transition journal commit
together; decision selection and rejection/timeout archival use transactions.
A killed process rolls back unfinished transactions. Identical contents share
compressed blobs. Overwritten mutable state keeps one current body; the audit
chain retains old/new hashes and metadata. Original immutable attempt keys and
sampling history keep their full contents. This is not full historical replay
of every overwritten poll payload.

Audit events have sequence numbers and chained SHA-256 hashes. Verification
checks chain continuity, the current namespace against the journal, and content
hashes. Preserve snapshot hashes externally when stronger evidence against
wholesale database replacement is required; a local hash chain is not a digital
signature. Submission intent commits before the external call and its receipt
commits before state advancement. Unknown outcomes require reconciliation;
restart never means submitting the same uncertain job again.

## Read, verify, and publish

Use the ordinary replay campaign status/audit/export interfaces. For individual
logical records:

```bash
uv run --all-packages python -m tricycle_sampling.replay.runtime_records read \
  --seed-dir /absolute/experiment/seed-00 --key state.json
uv run --all-packages python -m tricycle_sampling.replay.runtime_records list \
  --seed-dir /absolute/experiment/seed-00
uv run --all-packages python -m tricycle_sampling.replay.runtime_records verify \
  --seed-dir /absolute/experiment/seed-00
uv run --all-packages python -m tricycle_sampling.replay.runtime_records snapshot \
  --seed-dir /absolute/experiment/seed-00 --out /local/archive/seed-00.sqlite
```

Snapshots require a new local destination and include committed WAL contents.
For direct Python access use `record_io.read_bytes(logical_path)` or
`RecordLedger(database).read(key)`. Do not use `Path.read_text` on logical keys.
`replay.published` reads native containers without expanding a trajectory.

## Existing trajectory migration

Migration is explicit, including for active trajectories. Pause supervisors at
operation boundaries and stop concurrent readers. Do not terminate remote jobs.
Keep original experiment/model/input hashes and failure/recovery evidence.
Review source differences before authorizing a storage-only controller amendment;
never silently replace historical scientific bindings.

```bash
uv run --all-packages python -m tricycle_sampling.replay.runtime_records migrate \
  --seed-dir /absolute/experiment/seed-00
```

The seed lock excludes controller operations. Migration records every original
path/hash/mode/link, imports exact bytes in one transaction, verifies the audit
chain, and writes a standalone migration backup before publishing the database
reference. It compares remaining originals before removing expanded records.
Unexpected changes stop cleanup. Repeating the command finishes interrupted
pruning. Keep the immutable migration snapshot and original external archive.

New preparations record foundation persistence hashes separately from scientific
bindings. An existing source-bound experiment uses `live-records-amendment.json`
with schema `tricycle.live-records-amendment/v1`, original `experiment_sha256`,
nonempty `authorization` and `reason`, source review `evidence_path` and
`evidence_sha256`, `changes` mapping original controller paths to `before`/`after`
hashes, and current `record_backend_hashes`. Apply it after earlier audited
storage/decision amendments. Review and bind all new controller modules as well.
This receipt changes storage identity, not scientific acceptance or retry policy.

## Interrupted agent recovery

The inflight record, process identity, committed log chunks, and handoff location
survive controller failure. First establish that the recorded process stopped.
A completed pending JSON response may be imported without another model call:

```bash
uv run --all-packages python -m tricycle_sampling.replay.runtime_records recover-agent \
  --seed-dir /absolute/experiment/seed-00 --key round-0003/decision
```

Use `--review` for the review pass. The import checks the original task hash,
records an unknown exit status truthfully, and preserves the original invocation.
Normal controller validation still decides whether the response is acceptable.
Incomplete output is not accepted. Ordinary timeout handling retains evidence
and existing bounded restart opportunities; it does not consume formula repair
attempts or reset counters. Preserve unresolved handoffs for explicit recovery.

Historical sealed containers still use `replay.record_store verify/list/restore`.
Restoring one requires a new destination. Native containers use the commands
above. `record_store watch` supervises both layouts; finalization of a native
container verifies it without converting it to the historical sealed format.

Follow [run record scope](run-records.md). Offline figures and reproducible
comparison packages belong in `reports/` and reference source evidence.
