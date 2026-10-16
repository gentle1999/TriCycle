---
name: reaction-db-export-rxnresid
description: "Export native reaction thermodynamics and adapt ene/diene identities across mapping conventions, or export the GT RxnResid contract."
---

# Export RxnResid Mapped-Reaction Data

## Native thermodynamics export and substrate adaptation

For candidate/evaluation pool updates with ene/diene identities, prefer database `export_mapped_reaction_thermodynamics_csv`, without reconstructing labels locally or expanding rows from TS input provenance:

```bash
uv run --package tricycle-agent python -m tricycle_reaction_db.pools export \
  --config configs/database-sampling-pools.toml --live
```

Without `--live`, only an offline plan is generated, with no credentials or database access. Injected `ReactionThermodynamicsCsvAdapter.export_thermodynamics_csv(ReactionFilterRequest, destination)` downloads CSV, passes fixed reaction SMARTS as database `rxn_smarts` filter_expression, paginates by offset, validates project/count/cursor/field consistency, and preserves complete native CSV plus receipt.

Sampling dataset configuration requires `[export_filters] has_activation_gibbs_free_energy = true`. Pass this native database parameter on every page and retain it in `receipt.arguments`. Do not export unlabeled rows and delete them locally. Fail immediately if returned rows violate the filter. Optional `has_reaction_gibbs_free_energy` may also be supplied; activation-energy targeting does not require non-null reaction energy.

Reuse a native export offline with `adapt --config <config> --root <export-directory> --output-root <new-directory>`. This validates project-scoped receipt and original SHA-256 without querying the database.

Native CSV has one row per thermochemistry profile. Preserve all theory levels, temperatures, energies, and stage timings; do not select `profiles[0]`, minimum activation, or combine profiles. This differs from the dedicated six-column GT contract below and must not silently replace historical experiment labels.

Read the [dataset adaptation contract](../../../docs/reaction-dataset-adaptation.md) before execution. Reuse `mapping-independent-stereo-graph-v2`, not ad hoc reconstruction scripts. Parse stereochemistry with explicit hydrogens before ignoring map numbers; establish identity through bidirectional stereochemical matching of full molecular graphs. Connectivity strings only retrieve candidates, never prove pairing. Preserve E/Z, chirality, isotopes, charges, and radicals.

Revalidate old canonical-string aliases against the same project's original mapped-reactant/artifact bindings. Proxy pools cannot use GT aliases/labels; TS input IDs must not expand native rows. Add ene_id, diene_id, pair_id, and match status to every row while preserving original fields/count. Keep ambiguous/unmatched rows in the main table and also `unmatched.csv`; never guess or delete labels because matching failed. Write GT to evaluation and proxy pools to candidates.

Corrected native timings cover all recorded candidate calculations attributable to a mapping, including unconverged attempts, deduplicated by database source file. Total cost is the union of files across three stages, not their sum. Multiple profiles for one mapping repeat costs without creating extra charges. Preserve seconds and nulls; do not guess CPU/wall-clock semantics. Core-hour conversion and cross-mapping resource accounting require explicit provenance under the adaptation contract.

Receipts retain strategy/RDKit versions, source-row hashes, and matching evidence. Report input-pair coverage, database export completeness, and adaptation coverage separately. Investigate mapping/matched-pair changes through structural equivalence before claiming missing data. Changed adaptation policy uses a new output directory and new coverage assessment.

## Frozen sampling space

The active pin is referenced by `configs/frozen-sampling-space.toml`. Before subsequent experiments run `python -m tricycle_sampling.active_learning verify-frozen-db --config configs/frozen-sampling-space.toml` offline. Do not choose data via latest directories or new database queries; retain earlier experiments' snapshots.

New RxnResid experiments set `[dataset] frozen_space_config = "configs/frozen-sampling-space.toml"`. The loader binds four collections; validate/prepare rechecks hashes and records the space version.

When the user requests a new frozen space, use `python -m tricycle_sampling.active_learning freeze-db --root <native-export-directory> --snapshot-id <version-id> --pin-config <new-config-path>`. Never overwrite old pins/output directories. The native receipt must prove `has_activation_gibbs_free_energy=true`. Validate complete GT substrate Cartesian product, finite labels, and original-field preservation without local row selection/deletion.

Preserve the complete labeled native export and adaptation evidence under `audit/source-export`, including adaptation records and the structure registry. Current package code provides execution; fixed manifest SHA-256 and per-file hashes lock data version. Freezing does not query the database, choose minimum/mean profiles, or reconstruct labels. Create a new version pin for dataset changes and update the active reference only with user authorization, preserving old pins/snapshots.

Sampling registries contain only IDs/SMILES; source registries containing historical QM fields stay in evaluation/provenance. GT labels/evaluation results remain excluded from acquisition decisions. Experiments separately freeze a core-hour ledger including failed attempts.

## Dedicated GT RxnResid contract

This interface accepts only its implementation-designated GT project. Read `project_id` from the [request configuration](../../../configs/example-reaction-db-rxnresid-export.toml); other projects use native CSV export above. The GT ID is not a global default. Require `cleaned_only = true` and database actual-reaction `reaction_smarts`, never `reaction_class`:

```text
[#6,#7]=,:[#6,#7].[#6,#7]=,:[#6,#7]-,:[#6,#7]=,:[#6,#7]>>[#6,#7]1[#6,#7][#6,#7][#6,#7]=,:[#6,#7][#6,#7]1
```

## Database MCP queries

Execute `reaction-db.export-rxnresid` through injected `McpReactionDatabaseAdapter`, using MCP `compose_query`, not REST CSV routes:

1. Call `MappedReactionQueryService.list_mapped_reactions` with `project_id` and full `reaction_smarts`; consume all `items` using returned `page` metadata.
2. For each summary `id`, call `ReactionEnergyQueryService.get_mapped_reaction_thermodynamics` with the same project.
3. Use returned mapped SMILES and materialized profile. Energies and stage times must come from the same profile; do not recalculate, splice, or guess from logs.

Use the adapter's project-scoped query construction, not arbitrary GraphQL assembled in the skill. Every call uses the same `project_id`. Mapping queries take `reaction_smarts`, not `reaction_class`, extra `filter_expression`, or REST `has_*` parameters. Current `compose_query` complexity limit is 250; the current projection allows at most 24 thermodynamics queries per GraphQL alias batch. Split larger batches.

Project-scoped mapped reactions and materialized thermodynamics are authoritative cleaned data. Missing materialization never permits local-log fallback.

## Fields, units, and provenance

The six required CSV columns map as follows:

| MCP field | CSV column |
| --- | --- |
| `mapped_reaction_smiles` | `mapped_reaction_smiles` |
| `reaction.gibbs_free_energy_kcal_mol` | `reaction_free_energy` |
| `activation.gibbs_free_energy_kcal_mol` | `activation_free_energy` |
| `reactants_running_time_seconds` | `reactant_compute_seconds` |
| `transition_state_running_time_seconds` | `transition_state_compute_seconds` |
| `products_running_time_seconds` | `product_compute_seconds` |

Energy units are `kcal/mol`; timings are seconds. Leave null timings blank, never estimated. The current MCP adapter selects the first materialized profile, `profiles[0]`; all values in a row come from that profile, without mixing theory/temperature/profile fields. Missing or empty materialized profiles, or an invalid profile format, fail the export; do not query local files. Within a valid materialized profile, a missing reaction/activation object or required free energy excludes the row with a recorded reason and count. Required CSV columns remain present; never fabricate values.

Successful receipts declare `RXNRESID_EXPORT_API_CONTRACT` and retain `compose_query` provenance: MCP app name, service/method, `project_id`, unchanged `reaction_smarts`, mapping-page count, exported rows, `energy_unit = "kcal/mol"`, `timing_unit = "seconds"`, `cleaned_only = true`, and incomplete-thermochemistry exclusions. Never include bearer tokens. Failed MCP queries, returned errors, mismatched project IDs, or missing receipt evidence preclude success.

Read `TRICYCLE_REACTION_DB_MCP_URL`, `TRICYCLE_REACTION_DB_MCP_TOKEN`, and optional `TRICYCLE_REACTION_DB_MCP_APP_NAME` at runtime from the environment, never TOML credentials. Example request: `configs/example-reaction-db-rxnresid-export.toml`.

```bash
uv run --package tricycle-agent tricycle-agent run \
  reaction-db.export-rxnresid \
  configs/example-reaction-db-rxnresid-export.toml \
  --run-id rxnresid-ground-truth-export --live
```

Store CSV, receipts, and run records in `runs/YYYY-MM-DD/<local-timestamp>/record/`, replacing `runs/` with `TRICYCLE_RUN_ROOT` when set.

This record is the evidence for an individual export operation. Bulk reusable sampling-pool bundles produced by `tricycle_reaction_db.pools` instead default to `artifacts/datasets/`; downstream experiments reference their manifests. Keep independent comparisons and figures in `reports/`. Apply [run record scope](../../../docs/run-records.md) and preserve existing explicit export/recovery paths.
