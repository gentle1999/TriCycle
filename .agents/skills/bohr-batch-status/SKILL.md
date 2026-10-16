---
name: bohr-batch-status
description: "Query all submitted workers in a Bohr batch run and persist a status report in TOML-backed run state."
---

# Query Bohr Batch Status

The input is a run directory containing `run.toml`, `submit-results.tsv`, and public remote `BOHR_ID` values. Outputs are `status.tsv` and updated `run.toml`. This capability reads remote status only; it does not submit, download, or terminate workers.

The CLI's `id` is platform-internal and cannot be passed to lifecycle commands. Requests and receipts use public `bohrId`; no automatic internal-ID mapping or legacy-run migration is performed.

Inspect the directory first. Use the following only when `resume.next_action = "bohr.batch.status"`:

```bash
uv run --package tricycle-agent tricycle-agent resume \
  --run-dir 'runs/<date>/<timestamp>/<id>' --live
```

`resume` executes the recorded next action, not a fixed status command. If the next action differs, query independently with `BohrSubmitPlugin.status(run_dir, gateway, context)` and an injected gateway. Do not advance submission/download or rewrite state merely to query status.

Live queries produce `status.tsv`; dry-run makes no network call. Credentials may come from project `.env`, with shell variables taking precedence. Check `incomplete`, `failed`, and the next action; TSV reports are not configuration files.

## Failure recovery

Preserve `status.tsv` after transient network, credential, or individual-worker failures. Repair the environment and retry `resume --live`; state retains the next action. This capability does not submit or terminate workers.
