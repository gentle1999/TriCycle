---
name: reactionopt-config
description: "Generate a versioned, serializable ReactionOpt reaction configuration for downstream calculation."
---

# ReactionOpt Reaction Configuration

Use after sampling selects a candidate reaction, before generating conformers, reaction paths, or transition-state guesses.

## Inputs and workflow

Inputs are stable `reaction_id`, atom-mapped `reaction_smiles`, reaction mode/method (scaffold default `4+2`), conformer/force-field/path parameters, and optional output directory.

1. Validate reaction ID, mapped reaction SMILES, and method.
2. Call `ReactionOptPlugin.generate` for an in-memory configuration.
3. Call `plan("generate", ...)` to preserve versioned configuration and run context.
4. Use an explicitly injected `ReactionOptAdapter` to invoke the actual ReactionOpt project and export files.
5. Pass the configuration version and generated artifacts to Bohr or reaction-db workflows.

## Outputs and constraints

The current schema is `tricycle.reactionopt/v1`. By default only an in-memory configuration is generated; no RDKit/ASE/ReactionOpt computation runs. User requests and run state remain TOML. Do not infer complex reaction centers, conformer counts, or TS search parameters in the scaffold. The adapter and downstream plugin specification own actual ReactionOpt version/parameter compatibility.

Implementation: `packages/tricycle-reactionopt/src/tricycle_reactionopt/plugin.py`.
