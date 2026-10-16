# Reaction dataset adaptation

Entry points: `python -m tricycle_reaction_db.pools export / adapt`. The substrate-matching strategy is `mapping-independent-stereo-graph-v2`.

## Sources and record granularity

Use the dedicated database `export_mapped_reaction_thermodynamics_csv` through an injected adapter, paginated by project and fixed rxn-SMARTS. The database owns cleaning, reaction relationships, and thermochemistry; local code only adapts format and associates substrate identities. Preserve native CSVs, page receipts, and SHA-256 hashes. Do not reconstruct energies from calculation logs or guess why the database excluded records.

Sampling datasets explicitly use native `has_activation_gibbs_free_energy=true`, so the database excludes unlabeled activation-free-energy profiles during export. `[export_filters]`, every page request, and `receipt.arguments` record the same condition. Returned unlabeled rows fail validation instead of being filtered locally. Non-null reaction-free-energy filtering is an independent option.

Every native thermochemical profile row remains independent. Adaptation only appends fields and sorts by pair. `export_row_index` points back to the frozen original export row, enabling field-by-field checks that original SMILES, theory level, temperature, energies, and stage durations remain unchanged. Multiple profiles for one mapped reaction are neither merged nor reduced to minima/means. Mapped reaction IDs, mapping hashes, and RXN SMILES strings from different projects do not establish equivalence across versions.

## Substrate identity matching

- Atom-map numbers, atom traversal order, and ordering of the two reactants in RXN SMILES do not define substrate identity. New database conventions may renumber maps according to reactant traversal and full reaction correspondence; preserve the original mapped string unchanged.
- Parse the complete molecular graph with `removeHs=False`, retaining E/Z and chirality defined with explicit hydrogens. Clear map numbers, then remove ordinary hydrogens using RDKit hydrogen-removal operations. Isotopic hydrogens must not be removed as ordinary hydrogens. Do not remove hydrogens with default SMILES parsing before inferring stereochemical identity from written canonical SMILES.
- Connectivity strings are candidate lookup keys only. Final verification compares full graphs with equal atom and bond counts, performs bidirectional `useChirality` / `useEnhancedStereo` matching, and checks elements, isotopes, charges, radicals, hydrogen counts, and aromaticity. E/Z isomers, enantiomers, and unspecified stereochemistry retain distinct identities. Do not discard stereochemistry to reach a desired pair count.
- The strategy retains the declared oxide equivalence `X(+)-O(-) ↔ X=O`. It does not perform general Cleanup, tautomer merging, neutralization, or extra stereochemical reassignment. New chemical equivalences require versioning and validation of their scope.
- Prefer direct matches to original registry structures, checking both reactant orders. Only when direct matching fails may validated reactant/artifact bindings from the same project supply structural aliases. Aliases must not override an existing direct match. Default configuration uses only the registry and does not depend on alias caches from historical runs.
- Old canonical-string alias indexes are insufficient evidence. Reread `participants.mapped_smiles` from their original project-scoped queries and the substrate-file bindings of reactant geometries; record evidence-file hashes. Do not use TS input numbers, old approximate strings, GT labels, or GT aliases to fill proxy-pool pairs.

`pair_id` denotes the ene/diene identity established by this strategy. The original calculation input combination is separate provenance: it neither authorizes duplicating native rows nor replaces structural matching.

## Result states and integrity

`matched` requires a unique pair and a matching method. `ambiguous` retains all possible pairs without choosing arbitrarily. Keep `unmatched`, `outside_registry`, invalid SMILES, and non-two-reactant records in the main table and separately list reasons. `reactant_connectivity_keys` lacks complete stereochemical identity and must not be treated as match evidence. Each row records its strategy and method; receipts record RDKit version, source hashes, alias-binding hashes, and state counts.

Report these measures separately:

1. Original calculation-input coverage: the Cartesian product of ene and diene input sets.
2. Formal database export completeness: project, filters, pagination, record count, and native-file hashes.
3. Matching coverage under the current adaptation strategy: all rows, labeled rows, unique pairs, and failure reasons.

Fewer matched pairs than input combinations, or differences in old/new mappings and strings, trigger an adaptation investigation; they do not prove missing database data. First test graph equivalence, then explain differences using project-scoped database provenance. Assigning an ID to every row does not establish verified pairing. Old-version coverage statistics are not automatically conclusions for a new strategy.

## Isolation and replay

GT is stored under `evaluation`; proxy pools under `candidates`. Matching uses only structures and identities, not energies or evaluation results. Candidate-pool alias evidence comes only from its own project. GT labels and performance must not enter acquisition decisions.

Database stage durations retain original values and nulls. The four native `*_running_time_seconds` fields cover all recorded candidate calculations attributable to the mapping, including unconverged candidates with provenance. Endpoints are collected by participant topology; TS calculations by mapping geometry bindings and inference provenance. The database deduplicates by ArtifactFile and uses file-level duration from each file's latest source revision. Total duration deduplicates the union of files across all three stages again; it cannot be reconstructed by summing stage values. If any attributable file has unknown duration, the relevant aggregate remains null.

Different profiles of one mapping repeat the same cost; accounting must not sum it once per profile. Exports remain in seconds. The fixed experiment ledger separately specifies unit conversion and shared-resource accounting across mappings. Without evidence for CPU time or core counts, do not rename seconds as `core_hours`.

Existing formal exports can be replayed offline. Strategy updates use a new output directory and preserve old results:

```bash
uv run --package tricycle-agent python -m tricycle_reaction_db.pools adapt \
  --config configs/database-sampling-pools.toml \
  --root /absolute/path/to/native-export-run \
  --output-root /absolute/path/to/new-adaptation-run
```

Validation must cover explicit-hydrogen cyclic alkenes, map renumbering, nonidentity atom permutations, reactant reordering, enantiomers, isotopes, and negative examples with missing stereochemical specification. When generating test SMILES, also verify that serialization preserves the original graph. A stereochemical change introduced during RDKit writing must not be mislabeled as an equivalent input.

## Frozen sampling spaces

The current frozen space uses `configs/frozen-sampling-space.toml`, which points to a single snapshot manifest and pins its SHA-256. New experiments verify this pin offline before using the four collections specified by the manifest. Neither a `latest` directory nor a database query may substitute a different version. Historical experiments retain their own data and results.

```bash
uv run --package tricycle-agent python -m tricycle_sampling.active_learning verify-frozen-db \
  --config configs/frozen-sampling-space.toml
```

New RxnResid configurations set `[dataset] frozen_space_config = "configs/frozen-sampling-space.toml"`. The loader obtains GT and three candidate collections from the pin and rejects conflicting explicit collections. `validate` / `prepare` reverify the full snapshot and record its ID and manifest hash in experiment provenance. Each project's resources still require its complete core-hour ledger.

Generate a new pin only when the user explicitly requests another frozen space:

```bash
uv run --package tricycle-agent python -m tricycle_sampling.active_learning freeze-db \
  --root /absolute/path/to/verified-export \
  --snapshot-id selected-space --pin-config /absolute/path/to/new-pin.toml
```

Freezing requires native receipts proving activation-label filtering. Preserve all database-filtered rows without another local filter. Complete labeled native data and adaptation evidence remain under `audit/source-export`. Original fields and `export_row_index` stay unchanged. Do not select minimum/mean labels or deduplicate by shared TS. Verify equality between labeled GT pairs and the registry's full ene×diene set.

New snapshots retain data, adaptation records, and per-file hashes, without archiving executable source or overwriting existing pins. `substrate-registry.csv` contains only ene/diene IDs and SMILES. Historical QM fields from the original registry are archived only in `evaluation/provenance`, unavailable to sampling agents. Verification is an offline controller audit, not an acquisition input.

Corrected native durations already include attributable failed candidates. The experiment ledger separately fixes core-hour conversion, cross-mapping shared-resource accounting, and repeated profile costs; it does not reconstruct energy labels from the native table. Corrections to active dataset contents require a new snapshot version. Switching current configuration requires user authorization, with the previous pin retained and still verifiable.
