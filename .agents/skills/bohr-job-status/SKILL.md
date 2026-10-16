---
name: bohr-job-status
description: "Read the status of an existing Bohr job or prepared batch run and persist the next action."
---

# Query Bohr Job Status

Use when a run records the current CLI's public `BOHR_ID` or job group ID and its running, completed, or failed state needs checking. The platform-internal `id` in `bohr job list` is not a lifecycle job ID. Legacy batches need job group context for the adapter to resolve `bohrId`.

## Execution

For batches, follow [bohr-batch-status](../bohr-batch-status/SKILL.md). Inspect first; use the following only when `resume.next_action` is the intended `bohr.batch.status` or `bohr.job.status`:

```bash
uv run --package tricycle-agent tricycle-agent resume \
  --run-dir 'runs/<date>/<timestamp>/<id>' --live
```

For a single job, a separate TOML with `external_id` can be passed to `tricycle-agent run bohr.job.status request.toml --live` to create a new query record. Do not point `--run-dir` at a directory already containing `run.toml`. `resume` executes the recorded next action and is not necessarily a read-only query.

Dry-run makes no network request. Live queries require `--live` and Bohr environment variables; the project `.env` is discovered automatically. Results update state/reports, usually setting `next_action` to download after completion.

## Failure recovery

Inspect the preserved error state after transient failures, repair the environment, and retry with `resume --live`. Stop polling terminal jobs and follow `next_action` to download or manual handling.
