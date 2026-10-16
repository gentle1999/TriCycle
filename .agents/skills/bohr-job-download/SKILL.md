---
name: bohr-job-download
description: "Download completed Bohr artifacts into a run directory through the configured adapter."
---

# Download Bohr Results

Use after status confirms completion or when the user explicitly requests a download.

## Execution

A single-job TOML requires public `external_id` (the current CLI's `BOHR_ID`) and `destination`:

```bash
uv run --package tricycle-agent tricycle-agent run \
  bohr.job.download request.toml --run-id <id> --live
```

Inspect existing runs first; use `resume --run-dir <directory> --live` only when `resume.next_action` is the intended download capability. See [bohr-batch-download](../bohr-batch-download/SKILL.md) for batch downloads and report paths. Independent queries/downloads may create new operation records; do not call `run` again on an existing `run.toml` directory. Downloading reads remotely and writes locally: check the destination, preserve the download report, and keep credentials out of `run.toml`.

## Failure recovery

Downloads are validated in disposable storage before publication. Environment/cache
paths, including ZIP/tar members, reject publication without changing existing
results. An old remote package may still be transferred before this check because
the CLI has no member-level download filter. Preserve the error and remote job ID;
request a narrower remote result package instead of repeatedly downloading it or
rewriting an existing archive. Identical local files are reused; conflicts fail.

Preserve the destination and state on failure. Repair credentials, network access, job ID (which must be `bohrId`), or destination before `resume --live`. Check adapter overwrite/idempotency behavior before repeating a download; do not delete existing results.
