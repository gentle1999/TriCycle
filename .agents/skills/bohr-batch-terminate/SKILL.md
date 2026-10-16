---
name: bohr-batch-terminate
description: "Terminate selected incomplete Bohr batch workers only after explicit destructive-action confirmation."
---

# Terminate Bohr Batch Workers

Input is an existing batch run and an explicit worker selection. Outputs are a termination report and updated `run.toml`. This is the destructive batch capability; use other capabilities for status and downloads.

Query status and list the selected workers first. Dry-run only plans. Live execution requires all of:

- `--live`;
- `--confirm`;
- remote credentials injected through the environment or project `.env`.

Record termination results and public IDs in `run.toml`/reports. Do not execute without explicit confirmation.

## Failure recovery

State may retain the same next action after failure, but must not execute automatically without confirmation. Inspect the report and remote state, verify the target workers still match the existing authorization, then retry with both `--live --confirm`. An expanded target set needs new authorization; do not repeatedly ask about the same already-authorized operation.
