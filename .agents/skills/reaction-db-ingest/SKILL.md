---
name: reaction-db-ingest
description: "Validate reaction records or plan project-scoped calculation-log database workflows."
---

# Reaction Database Workflows

Use to ingest reaction records or import Bohr calculation logs by batch. Use `reaction-db-batch-import` for large log batches and the corresponding reaction-db skills for filtering/training-data export.

## Boundaries and inputs

Dry-run only validates/plans and never writes remotely. Live batch import requires an application-injected `ReactionDatabaseAdapter`, which creates the database project and returns its internal `project_id`. Keep credentials out of requests and run state.

Single-reaction requests contain `reaction_id`, `reaction_smiles`, optional `conditions`, `provenance`, and artifacts. Batch requests contain exactly one project-creation description and batch directory, recursively selecting only `*.log` under `downloads/`. Preserve workflow, initial-guess source, and Bohr job group/project provenance.

Use `ReactionIngestRequest` for single records, retaining `idempotency_key`, provenance, and artifacts. Actual writes require injected `ReactionRepository.upsert` with dry-run disabled; `validate_only=True` only validates. The default CLI supplies no live single-reaction repository. Batch MCP adapter support does not imply live single-record upsert support. Conflict merging follows the repository contract.

Exports use internal database `project_id` and database-authoritative filtering of successfully cleaned data with this actual-reaction `reaction_smarts`, never `reaction_class`:

```text
[#6,#7]=,:[#6,#7].[#6,#7]=,:[#6,#7]-,:[#6,#7]=,:[#6,#7]>>[#6,#7]1[#6,#7][#6,#7][#6,#7]=,:[#6,#7][#6,#7]1
```

RitS needs `mapped_reaction_smiles + ts_structure`; RxnResid needs `mapped_reaction_smiles + reaction/activation free energy + reactant/TS/product seconds`.

For candidate/evaluation pools, use the native CSV and existing adaptation entry points in [reaction-db-export-rxnresid](../reaction-db-export-rxnresid/SKILL.md). Validate substrate identity under the [dataset adaptation contract](../../../docs/reaction-dataset-adaptation.md), preserving native profile rows and stereochemistry. Investigate graph equivalence when mapping/traversal changes; matching coverage does not establish export completeness.

Keep `run.toml` as the application audit entry point. Do not invent database schema or reconstruct reaction relationships locally.

## Failure recovery

Correct validation failures in TOML and create a new run. After remote write failure, inspect whether idempotent project creation/import already completed, then recover through the adapter-owning application. Never repeat writes blindly without adapter state.

## Query layers

Use artifact/ingestion for raw logs and parsing state; formula/topology/geometry for chemical identity/structures; calculation protocol/segment/frame/result for computation; transition state inference for TS evidence; logical/mapped reaction for relationships; reaction energy for free/activation energy; workflow manifest for batch/artifact bindings. Pagination, project scope, and provenance belong to the adapter; TriCycle does not duplicate database query schemas.
