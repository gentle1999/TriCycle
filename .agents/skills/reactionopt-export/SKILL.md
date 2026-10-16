---
name: reactionopt-export
description: "Export a generated ReactionOpt configuration through an explicitly injected local adapter."
---

# Export ReactionOpt Configuration

Use only with an explicit `ReactionOptAdapter` and destination. Requests remain TOML. Dry-run only plans and does not invoke the actual ReactionOpt project. Real export requires application-injected adapter configuration and output artifact locations recorded in `run.toml`. Do not directly import an external project or hardcode paths/credentials in this skill.

## Failure recovery

Inspect errors and output locations before repairing the adapter and retrying with a new `run-id` and explicit destination. The adapter contract does not guarantee safe overwrites: do not blindly resume, guess ReactionOpt parameters, or overwrite existing output directories.

## Scope

Without an explicit adapter or destination, use `reactionopt.generate`. This skill does not generate reaction parameters or submit Bohr jobs.
