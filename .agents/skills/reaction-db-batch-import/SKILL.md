---
name: reaction-db-batch-import
description: "Plan one new reaction-database project from exactly one Bohr calculation batch."
---

# Import One Reaction Database Batch

Each request permits one new database project and exactly one Bohr batch. The adapter first creates the project, then imports recursively discovered `*.log` files under `logs_dir`. Successful results must contain the database-generated `project_id`; a local slug is not a project ID. Do not append a second batch to the same request.

Record `provenance.workflow` and `provenance.initial_guess_source`; Bohr batches also require `bohr_job_group_id` and `bohr_project_id`. The default directory is `downloads/`. Selection is limited to logs: `.gjf`, `.json`, `.xyz`, and worker `.out` files are not uploaded.

Review log count, total bytes, batch fingerprint, and project metadata in the dry-run plan:

```bash
uv run --package tricycle-agent tricycle-agent plan reaction-db.batch-import request.toml
```

Live import requires explicit injection of `ReactionDatabaseAdapter`. The supplied `McpReactionDatabaseAdapter` uses the database Streamable HTTP MCP API; the default CLI constructs/injects it from environment variables only during `--live` execution. Default paths read no database credentials, access no network, and create no project. Two batches require two requests and two internal project IDs.

Live MCP configuration requires `TRICYCLE_REACTION_DB_MCP_URL`, `TRICYCLE_REACTION_DB_MCP_TOKEN`, and `TRICYCLE_REACTION_DB_ORGANIZATION_ID`. The adapter calls MCP `create_project`, then groups uploads according to service limits and sends log bytes through multipart `POST /api/upload-batches` and `POST /api/upload-batches/{batch_id}/files`. These interfaces receive files and queue descriptions; the database handles RustFS staging and parsing through the same durable worker as single-file ingestion. Even colocated deployments must not substitute shared directories/path manifests.

`staged`, `queued`, and `processing` mean files entered the durable database queue; receipt status is `submitted`. Import submits the complete queue without waiting for MolOP parsing. Do not claim unavailable `ingestion_status` or cleaned-reaction counts as results. Preserve each batch/item ID, queue state, and failure sample in receipts. TLS verification is on by default.

Implementation: `packages/tricycle-reaction-db/src/tricycle_reaction_db/models.py`, `plugin.py`, and `mcp_adapter.py`.
