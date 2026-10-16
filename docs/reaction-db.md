# Reaction database workflow constraints

`tricycle-reaction-db` declares input boundaries, creates audit plans, and defines adapter contracts. The database owns log parsing, molecular/topological/geometric relationships, TS evidence, reaction relationships, and final cleaning. TriCycle does not duplicate those domain rules locally.

## Projects and batches

One `reaction-db.batch-import` request creates one internal database project and submits logs from one Bohr calculation batch. On success, the adapter must return the internal `project_id`. Two Bohr batches therefore require two requests and produce two project IDs. Filtering and export must use the corresponding project ID.

Batch discovery recursively selects only `*.log` under `downloads/`. Input `.gjf`, metadata `.json`, structure `.xyz`, worker `.out`, and other batch directories are outside the upload scope. Requests require this lineage:

- `provenance.workflow`;
- `provenance.initial_guess_source`;
- source-batch `bohr_job_group_id` and `bohr_project_id`;
- an automatically generated or explicitly supplied idempotency key.

## Core operations

| Capability | Input | Database behavior | Minimum output contract |
| --- | --- | --- | --- |
| `reaction-db.batch-import` | One project description and one batch | Create project and enqueue logs for durable parsing | `project_id`, queue receipt, initial status; not proof of parsing completion |
| `reaction-db.filter` | `project_id` and fixed `reaction_smarts` | Query actual reaction SMARTS and successfully cleaned data only; no `reaction_class` | Match count, actual SMARTS, query provenance |
| `reaction-db.export-rits` | `project_id` and CSV path | Export mapped reactions and TS structures from the same filtered results | `mapped_reaction_smiles`, `ts_structure` |
| `reaction-db.export-rxnresid` | `project_id` and CSV path | Export reaction energies, activation energies, and stage durations | Mapped reaction, both free energies, precursor/TS/successor seconds |

The database rxn-SMARTS predicate is fixed:

```text
[#6,#7]=,:[#6,#7].[#6,#7]=,:[#6,#7]-,:[#6,#7]=,:[#6,#7]>>[#6,#7]1[#6,#7][#6,#7][#6,#7]=,:[#6,#7][#6,#7]1
```

Adapters must pass `reaction_smarts` unchanged to the database filter. They must neither replace it with a `reaction_class` condition nor classify reactions locally. RitS TS structures default to `xyz`. RxnResid uses database MCP `MappedReactionQueryService.list_mapped_reactions` with pagination by `project_id` and the full `reaction_smarts`, reading materialized thermodynamics profiles for each mapped reaction. Energies are in kcal/mol; stage durations are in seconds. Returned project IDs are saved as run provenance rather than hard-coded in shared workflows.

Without an explicit destination, CSV and `receipt.json` are written under `$TRICYCLE_RUN_ROOT/YYYY-MM-DD/<local-timestamp>/record/`; an unset run root defaults to `runs/`. The injected `ReactionDatabaseAdapter` owns CSV serialization, database queries, and downloads.

The built-in `McpReactionDatabaseAdapter` creates projects through the database's Streamable HTTP MCP endpoint and submits logs through its batch file API: first MCP `create_project`, then `POST /api/upload-batches` to create queues and `POST /api/upload-batches/{batch_id}/files` for multipart uploads within service limits. General filtering, RitS geometry exports, and RxnResid thermodynamics exports use MCP `compose_query` / paginated queries. Batch and single-file endpoints share database RustFS staging and durable workers. TriCycle transfers only file bytes and queue descriptions; it requires no shared staging directory visible to the service and does not change implementation for colocated deployments.

Each batch response returns durable batch/item records. The adapter returns `submitted` after all files are enqueued, retaining queue IDs and initial state. It does not wait for asynchronous MolOP parsing or fill in cleaned-reaction counts during submission. Failed files/batches are recorded in the receipt while remaining inputs are still attempted.

The live CLI injects this adapter only during execution; dry runs do not read database credentials. Supply `TRICYCLE_REACTION_DB_MCP_URL`, `TRICYCLE_REACTION_DB_MCP_TOKEN`, and `TRICYCLE_REACTION_DB_ORGANIZATION_ID` through the environment. Configure `TRICYCLE_REACTION_DB_MCP_CA_BUNDLE` for an internal CA. Do not disable TLS verification by default to bypass certificate problems.

## Standard database export gates

### Native thermodynamics CSV and substrate adaptation

To update candidate/evaluation pools, use `python -m tricycle_reaction_db.pools export` with `configs/database-sampling-pools.toml`. The default is an offline plan; only `--live` constructs the database adapter. Adapter `export_thermodynamics_csv` calls the dedicated `export_mapped_reaction_thermodynamics_csv`, passing the fixed SMARTS through the database's `rxn_smarts` filter.

Sampling exports also pass native `has_activation_gibbs_free_energy=true` on every page. `[export_filters]` and `receipt.arguments` preserve this condition. Fail if an unlabeled profile is returned rather than substituting local filtering. The condition must apply to every returned profile row. Page verification stays in the export run; see [frozen sampling spaces](frozen-sampling-space.md) for freezing and reuse. Native CSV contains one row per materialized profile, retaining full theory level, temperature, energies, and durations. It does not perform the older RxnResid `profiles[0]` label selection.

Substrate adaptation reuses stereochemical graph matching in `tricycle_reaction_db.substrate_adaptation`. It only appends ene/diene identities and match status, preserving native fields, row counts, and multiple profiles. Only identity and structure columns are read from source substrate tables. Optional aliases must come from validated reactant structures in the same project, not from re-expanding TS input combinations. GT and candidate pools remain in separate directories; nonunique matches are retained and listed separately.

Corrected database durations cover all recorded candidate calculations attributable to the current mapping, including unconverged attempts, deduplicated by source file. Profiles repeat the mapping-level cost. The experimental ledger defines core-hour conversion and shared-resource accounting across mappings. Follow the [dataset adaptation specification](reaction-dataset-adaptation.md) for matching, versioning, and integrity criteria. `mapping-independent-stereo-graph-v2` uses complete molecular graphs and preserves stereochemistry defined by explicit hydrogens. Old canonical-string pairing statistics cannot be inherited directly; fewer matched combinations do not establish missing data.

Export adapters must not assemble arbitrary local log-parsing results into a supposedly successful CSV. Successful `ReactionSpaceExportReceipt` objects require `api_contract` and standard-request evidence, validated before the plugin creates a `TaskResult`. RitS MCP evidence records `compose_query` filtering, paginated geometry-export tools, project ID, and fixed reaction SMARTS. RxnResid evidence records filtering/thermodynamics services, pagination and alias batches, project ID, and fixed reaction SMARTS.

The RxnResid export contract reads filtered database results through paginated `compose_query` and takes energies and stage durations from the same `profiles[0]`. Each thermodynamics alias batch contains at most 24 items. Rows missing required energies are excluded with recorded reasons; missing or invalid profiles terminate export. RitS calls `MappedReactionQueryService.list_mapped_reactions` through `compose_query` for database-authoritative SMARTS filtering, then uses cursor-paginated `export_mapped_reaction_transition_state_geometries` for mapped SMILES and TS geometries, materializing XYZ in atom-map order. The MCP adapter must not use `reaction_class` or local log inference.

## Basic queries

Basic queries are read-only, project-scoped, and preserve database cursors and provenance:

| Query layer | Purpose |
| --- | --- |
| project / artifact / ingestion | Verify project scope, source logs, uploads, and parsing status |
| formula / topology / geometry | Inspect molecular identity, topology deduplication, and reusable structures |
| calculation protocol / segment / frame / result | Inspect methods, pathway stages, frames, energies, and calculation results |
| transition-state inference | Inspect imaginary frequencies, endpoint changes, and TS inference evidence |
| logical / mapped reaction | Inspect reaction relationships, mappings, and pathway nodes |
| reaction energy | Inspect reaction/activation free energies and thermodynamic summaries |
| workflow manifest / artifact binding | Trace bindings among batches, files, and workflow versions |

TriCycle describes these purposes without hard-coding remote GraphQL/MCP schemas in core. Adapters implement concrete MCP queries. Prefer database-provided batch exports over locally assembled CSVs.

## Batch isolation

Each calculation batch uses a separate `reaction-db.batch-import` request and creates a distinct database project. Later filtering/export references only the corresponding project ID. See the [reaction-db-batch-import skill](../.agents/skills/reaction-db-batch-import/SKILL.md) for fields, idempotency, and execution boundaries.
