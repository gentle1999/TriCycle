---
name: reaction-db-export-rits
description: "Export mapped reaction SMILES and transition-state structures for RitS."
---

# Export a RitS Dataset

Use an explicit internal `project_id` and database MCP filtering with this actual-reaction `reaction_smarts`, then export CSV. Do not use `reaction_class` or derive mapped reactions/TS structures from local logs:

```text
[#6,#7]=,:[#6,#7].[#6,#7]=,:[#6,#7]-,:[#6,#7]=,:[#6,#7]>>[#6,#7]1[#6,#7][#6,#7][#6,#7]=,:[#6,#7][#6,#7]1
```

Each row requires `mapped_reaction_smiles` and corresponding `ts_structure`, default format `xyz`. If the project ID is unknown, query MCP `list_projects` and select the intended ground-truth project. The dedicated RxnResid export project ID is not a RitS default.

## Database pagination and filtering

Use MCP `export_mapped_reaction_transition_state_geometries` for mapped reactions and verified TS geometries. It returns bounded pages with `after_binding_id`, `records`, `has_more`, and `next_after_binding_id`. Record `key` is mapped reaction SMILES; `value.geometry.atoms` contains angstrom coordinates and `atom_map_number`. Streamable HTTP is transport, not this application-level export operation; do not substitute a same-name REST route.

Supported geometry schemas are `mapped-reaction-ts-geometry-v1` and `v2`. Additional v2 metadata does not replace `geometry_id`, `coordinate_units`, or `atoms`. Validate the same core fields and atom-map contract rather than discarding records merely because the schema version changed.

The geometry tool paginates by project/cursor and accepts no `reaction_smarts`. Also call MCP `compose_query` with `MappedReactionQueryService.list_mapped_reactions`, the same complete `project_id`, and fixed `reaction_smarts`. Page through the database-approved mapped-SMILES set, then keep geometry records whose `key` is in that set. Do not reinterpret SMARTS locally or replace database filtering with `reaction_class` or logs.

Process each geometry page without accumulating all XYZ. Validate schema/project scope, angstrom units, and unique complete maps `1..N`; sort elements/coordinates by increasing `atom_map_number` before materializing XYZ. Preserve duplicate mapped SMILES because one reaction may bind multiple TS geometries. After all pages, fail if any approved reaction lacks verified geometry; do not silently omit it or guess atom order.

Stream rows into a temporary local CSV with columns `mapped_reaction_smiles` and `ts_structure`, then atomically replace the destination only after full success. Receipt must record `reaction-db.mcp.mapped-reaction-geometry.v1`, provenance for filtering `compose_query` and geometry export, cursor-page count, scanned records, and exported rows. If the deployment lacks the geometry tool, fail explicitly; no REST fallback or serial per-reaction queries. `create_units_ts_dataset` exports UniTS NPY and is not the RitS contract.

## Execution and training handoff

Record project ID, filters, structure format, row count, and MCP provenance. Dry-run neither queries nor writes files. Live execution requires application-injected `McpReactionDatabaseAdapter` and environment-provided endpoint/token. Review the plan before live export:

```bash
uv run --package tricycle-agent tricycle-agent plan \
  reaction-db.export-rits request.toml --run-id rits-export

uv run --package tricycle-agent tricycle-agent run \
  reaction-db.export-rits request.toml --run-id rits-export --live
```

This CSV is authoritative source data, not a ready-to-train `rits.train` directory. Preprocess using the selected RitS `source_revision` contract before planning training. If required, create `AAM`, concatenated XYZ corpus, or adjacent forward/reverse samples, preserving the same TS geometry for each pair. Check reactant/product element agreement at every map and XYZ element order after ascending-map sorting. Record each incompatible sample in a rejection list and manifest counts; do not silently discard, guess mapping, or rename CSV as preprocessed data.

Implementation: `packages/tricycle-reaction-db/src/tricycle_reaction_db/models.py`, `plugin.py`, `repository.py`, `mcp_adapter.py`, and `contracts.py`.
