---
name: bohr-batch-submit
description: "Submit a prepared Bohr batch run through the configured Bohr adapter after explicit approval."
---

# Submit a Bohr Batch

Use only for a prepared run with explicit user authorization for remote submission.

## Preflight

- Confirm `run.toml`, `batch.toml`, and `submit-plan.tsv` exist, workers are nonempty, and image, project, and machine match the intended plan.
- Recheck `batch.toml` engine/machine_type: DPA requires GPU; pure Gaussian requires CPU. Do not override resources with extra CLI arguments.
- ReactionT5 batches referencing a frozen SAR protocol must upload complete local assets. Validate weights and tokenizer/config hashes before submission; reject missing assets rather than falling back to remote HF downloads.
- Supply `BOHR_ACCESS_KEY` and other credentials through environment variables or project `.env`, never TOML. Shell variables take precedence.
- Require explicit result selectors in `backward_files`: named output directories
  or file patterns such as `results/`, `worker.log`, and `*.xyz`. Whole-workspace
  selectors, recursive globs, wildcard directories, and environment/cache paths
  are rejected. Input bundles must pass the shared transfer checks; extra CLI
  arguments cannot override validated input paths or result-selector config.

## Execution

Inspect the state, confirm `resume.next_action` is `bohr.batch.submit`, and dry-run that action:

```bash
uv run --package tricycle-agent tricycle-agent inspect --run-dir /absolute/prepared-run
uv run --package tricycle-agent tricycle-agent resume --run-dir /absolute/prepared-run
```

Within existing submission authorization, add `--live` to the same `resume` command. If needed, select adapter policy with `--bohr-config configs/example-bohr.toml`. Do not use `run` to recreate a directory containing `run.toml`; once state advances to status/download, `resume` is no longer a submission entry point. Submission results and public job group ID are saved in `run.toml`; follow `resume.next_action` for status.

## Failure recovery

After gateway failure, inspect `run.toml` and `submit-results.tsv`, verify credentials/project/image, then use `resume --live`. Successfully recorded workers are reused rather than resubmitted; only unfinished workers are retried.

## Direct networking

For each CLI subprocess, the adapter removes HTTP_PROXY, HTTPS_PROXY, ALL_PROXY (including lowercase forms) and sets NO_PROXY/no_proxy=*. Job APIs and signed uploads/downloads connect directly. The parent process environment and Codex decision-request proxy are unchanged. Reload long-lived supervisors after their current operation finishes.
