# Frozen sampling spaces

A frozen sampling space binds immutable data and identity-adaptation results; it does not contain historical executable source. The current pin is [frozen-sampling-space.toml](../configs/frozen-sampling-space.toml). Read snapshot IDs, source paths, file hashes, and actual sizes from that configuration and its manifest. General documentation does not record an individual export's counts, coverage, or verification scores.

## Exporting and freezing

Export native data through the [database pool configuration](../configs/database-sampling-pools.toml) and `tricycle_reaction_db.pools`. Matching rules are defined in [dataset adaptation](reaction-dataset-adaptation.md). Every database page applies the non-null activation-free-energy filter, and receipts must record the same arguments. Fail on returned unlabeled rows rather than silently dropping them locally.

Reusable database pool exports and frozen spaces default to `artifacts/datasets/<date>/<timestamp>/<name>/`. They are input assets for multiple experiments, not new execution runs. Explicit output roots and existing pins remain unchanged; moving existing snapshots requires a separate verified migration. See [run record scope](run-records.md).

Create a new pin from a verified export only after explicitly selecting a new data space:

```bash
uv run --package tricycle-agent python -m tricycle_sampling.active_learning freeze-db \
  --root /absolute/verified-export --snapshot-id selected-space \
  --pin-config /absolute/new-pin.toml
```

Freezing preserves all native profile rows, original fields, and `export_row_index`, while recording identity-adaptation evidence, file sizes, and hashes. Labeled GT pairs must cover the registry's ene×diene set. Do not take minimum/mean labels, derive pairs from TS inputs, or fill in labels locally. New output must not overwrite an existing pin.

GT lives under `evaluation`, proxy pools under `candidates`. The registry visible to sampling contains only IDs/SMILES. Historical QM fields from the original registry remain in separate evaluation provenance and are not supplied to acquisition decisions. Database durations retain native seconds and nulls. The experiment ledger explicitly defines core-hour conversion, shared-resource accounting, and duplicated profile costs.

## Use and verification

```bash
uv run --package tricycle-agent python -m tricycle_sampling.active_learning verify-frozen-db \
  --config configs/frozen-sampling-space.toml
```

New experiments set `[dataset] frozen_space_config` to the selected pin. The loader reads the specified GT and candidate collections and rejects explicit overrides that conflict with the pin. Validation/preparation rechecks the snapshot and records its ID and manifest hash. Project cost ledgers still require explicit bindings.

Missing files or hash mismatches require stopping and checking provenance. Never switch automatically to the latest export or refresh historical hashes. Snapshot file-integrity verification does not establish a cross-page database transaction snapshot or operating-system write protection.

Actual export receipts, pagination verification, coverage statistics, and duration comparisons stay in their run/snapshot, outside source commits. Preserve superseded pins using the [configuration archive rules](../configs/README.md); current workflows must not depend on local archives. Do not rewrite existing experiment data, results, or checkpoints.
