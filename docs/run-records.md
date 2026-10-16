# Run record scope and output destinations

`runs/` is the execution ledger for identifiable tasks. It is not the default
destination for every generated file. Admit a record only when its owning task
and its role in execution, result verification, or recovery are clear.

## Admission rule

Before creating a run directory or adding a file, establish:

1. **Task identity:** which requested computation, preparation, import/export,
   sampling campaign, or other domain operation owns it?
2. **Authoritative entry point:** which existing request, plan, state, or manifest
   identifies that task and binds its inputs and protocol?
3. **Execution need:** is this file required to execute, resume, validate, or
   explain the outcome of that task, including a failure?
4. **Single source:** can the task reference an existing immutable asset or
   authoritative record instead of copying it?

If the first three answers are missing, choose another destination. A timestamp,
an experiment-related filename, or a `.json` extension does not qualify a file.
Use the owning module's existing record schema; do not add a second universal
run manifest or duplicate summaries merely to satisfy this rule.

## Destinations

| Material | Destination | Boundary |
| --- | --- | --- |
| Task requests, plans, state, receipts, execution logs, validated results, and recovery evidence | `runs/<date>/<timestamp>/<run-id>/` | One identifiable execution or prepared task; use its existing manifest |
| Cross-run comparisons, exploratory statistics, figures, manuscript tables, and publication result packages | `reports/<topic>/<report-id>/` | Reference source runs and hashes; do not copy whole runs |
| Reusable database pools, frozen sampling spaces, data splits, model assets, and worker assets | `artifacts/datasets/`, `artifacts/model-assets/`, or the configured shared asset store | Run records contain references and hashes; module-specific storage contracts remain authoritative |
| Repository-wide audits, repair source backups/diffs, migration inventories, and retained development diagnostics | `artifacts/audits/<audit-id>/` or an existing dedicated artifact archive | Keep only the receipt and immutable evidence reference needed for an affected run inside that run |
| Archived runs and their inventory/checksum receipts | `artifacts/local-run-archives/` or explicitly configured external archive storage | Verify recoverability and shared-asset dependencies before removing originals |
| Balance/quota monitoring, synchronization settings, and other host-service state | `.tricycle/<service>/` | A service watching experiments is not itself an experiment |
| Disposable tests, smoke probes, staging, scratch scripts, and exploratory debugging | OS temporary directories or tool-managed caches | Persist only explicitly requested diagnostic evidence under `artifacts/validation/` or `artifacts/audits/` |
| Maintained code, operational instructions, and reusable configuration | Owning package, `analysis/`, `tools/`, `docs/`, `.agents/skills/`, or `configs/` | Never turn `runs/` into a second source tree |

All generated destinations above are outside source commits. `analysis/` contains
analysis code and its environment, not report output.

## What belongs in an execution record

- The actual request and protocol/data/source bindings, without credentials.
- Submitted job identities and receipts, state transitions, stage-specific logs,
  and the supervisor files needed to operate this task.
- Candidate decisions, validation evidence, accepted/rejected outputs, timeout
  history, and checkpoints/results required by the owning module's contract.
- Failure and recovery receipts, with authorization and before/after bindings
  when a protocol intervention is explicitly authorized.
- Compact current status or result summaries produced by the controller. Detailed
  retrospective analysis and alternative report layouts belong in `reports/`.

Prepared-but-not-submitted tasks qualify when they retain a real request and a
durable execution plan that can be inspected or submitted later. Ad hoc dry-run
probes and smoke tests use temporary directories. Failed and interrupted tasks
remain valid execution records; their evidence must not be deleted just because
the task did not succeed.

Reuse the existing run for status queries, downloads, retries, and recovery.
Within a replay trajectory, maintain one current controller state and one compact
operational event journal at the seed root. Do not persist full state snapshots
per poll or copy trajectory state into each submitted round. Keep round-specific
execution receipts and scientific evidence with their owning rounds.
Use [live replay containers](compact-replay-records.md) from the first write:
logical round paths remain indexed inside one transactional container per seed.
Keep live SQLite storage local and durable; transfer consistent closed snapshots.
Existing records require explicit migration with paused controllers, preserved
original bytes, and reviewed source bindings. Do not silently migrate evidence.
Do not create another run for each inspection or copy an entire task into a
comparison report. Suites may contain child tasks under their authoritative
suite/campaign manifest. Do not create free-floating notes or folders at the
`runs/` root, date level, or timestamp level.

Only preparation-frozen worker delivery governed by a module's storage contract
is execution material. Arbitrary checkout snapshots, patched controller copies,
scratch scripts, test fixtures, and environment caches are not. Store independent
repair evidence outside the run and retain its path and hash in the run's audit
receipt. Never execute an archived implementation to continue a task.

Installed virtual environments and package/tool caches are not transfer artifacts.
Adapters validate new uploads and downloaded results before publication, including
ZIP/tar member names. Reject contaminated bundles with an actionable path instead
of silently changing their dependencies or rewriting historical archives. Runtime
source, dependency declarations, and launch scripts required for execution remain
valid inputs. Result selection must name outputs rather than the whole workspace.

## New writes and historical records

Use this policy for new default destinations and new agent-authored records.
An explicit `run_dir` or `out_dir` remains an API contract, especially during
recovery; it must not be silently redirected. For new work, select the destination
by purpose before passing that argument. Preserving explicit paths does not make
unrelated reports or development debris eligible for `runs/`.

This policy does not authorize cleanup or relocation of existing records,
compressed archives, shared assets, or active synchronization files. Existing
absolute paths, hashes, service definitions, and recovery evidence may depend on
them. A separate migration must inventory dependencies, preserve original bytes
and provenance, verify the destination, and update references through the owning
interfaces. Do not unpack archives merely to enforce a new directory preference.

## Review checklist

When changing a writer or running an agent task, check the admission rule and
destination table, reuse established manifests, and document the output root in
the owning README or skill. Verify that temporary validation leaves no new
production run, that explicit paths remain usable, and that existing recovery
records are untouched. This is a semantic ownership rule, not an extension or
filename blacklist.
