---
name: bohr-batch-watch
description: "Poll a prepared Bohr batch run through the adapter until completion or a configured limit."
---

# Watch a Bohr Batch

Inputs are submitted `run.toml`/batch reports and polling parameters. Outputs are continuously updated `status.tsv` and run state. The capability queries remote status and, when `download_when_complete = true`, downloads results locally after completion. It does not submit or terminate jobs.

Use only submitted runs. Set a reasonable polling interval and maximum iteration count; persist long-running state in `run.toml` rather than relying on chat history. Dry-run does not poll remotely. Live watching requires `--live` and Bohr environment variables, optionally loaded from project `.env`.

For one query use `bohr.batch.status`; use a download capability after completion.

## Failure recovery

After `max_iterations` or a transient gateway error, inspect first. Continue with `resume --live` only if `resume.next_action = "bohr.batch.watch"`. Completion may change the next action to download, so `resume` cannot always be treated as watch.

Change polling parameters through explicit arguments to `BohrSubmitPlugin.watch` with an injected gateway. Existing run requests are immutable: do not edit original TOML or recreate the directory with `run`.
