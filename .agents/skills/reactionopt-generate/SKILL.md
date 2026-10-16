---
name: reactionopt-generate
description: "Generate a versioned ReactionOpt configuration from a TOML reaction request without external side effects."
---

# Generate ReactionOpt Configuration

Use an existing reaction SMILES, reaction ID, and method parameters to generate a versioned configuration for downstream components.

Input is TOML containing at least `reaction_id` and `reaction_smiles`. Output is `TaskResult.metadata.config`, including `schema_version`. Default execution reads no credentials, accesses no network, and writes no remote system.

```bash
uv run --package tricycle-agent tricycle-agent run \
  reactionopt.generate request.toml --run-id <id>
```

To plan first, use `tricycle-agent plan reactionopt.generate request.toml`. Do not directly import component internals. Export files through the export capability with an explicitly injected `ReactionOptAdapter`.

## Routing and failure recovery

Use `reactionopt.export` to invoke the actual ReactionOpt project or write an external destination. This capability does not access the network or submit Bohr jobs. Correct invalid TOML/requests and retry with a new `run-id`; this side-effect-free capability generally needs no later resume.
