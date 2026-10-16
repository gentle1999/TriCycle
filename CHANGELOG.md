# Changelog

This file records software changes, not current deployment status or individual experiment results. Package READMEs describe current interfaces; configuration and asset manifests hold version bindings.

## 2026-09-14

### Fixed

- `bohr.batch.download` no longer skips downloads solely because a worker has overall status `FAILED`. It attempts to retrieve successful partial tasks, logs, and diagnostics from failed workers and records each worker's download result.

### Added

- Added `McpReactionDatabaseAdapter` to `tricycle-reaction-db`, using the database's Streamable HTTP MCP project, manifest, import-job, and compose-query tools for import, filtering, and export. MCP import uses staging paths visible to the database service and file hashes rather than transmitting log bytes through MCP.
- Live agent execution automatically injects the MCP adapter. Database MCP URL, token, organization ID, staging root, TLS policy, and timeout policy are read only from environment variables. Dry runs do not access the network.

## 2026-09-13

### Added

- Added run traces based on JSONL, hash chains, and file locks, with integrity verification for any snapshot.
- Added reproducible exploration rounds recording the complete candidate pool, model scores, sampling order and exclusions, next-round search-space decisions, RitS train/skip decisions, and snapshot transitions.
- Added stable replay keys for train, finetune, and skip transitions so that the same snapshot and round decision reproduce the next snapshot; QM calculations are treated as repeatable for the same inputs.
- Added CLI run/resume records for requests, plans, capability execution, and trace validation, plus safe reuse of exploration rounds.
- Added predictive, epistemic, and aleatoric uncertainty sampling strategies and deterministic candidate-score records.
- Added source bindings to RxnResid model assets, recorded native evidential uncertainty outputs, and enabled sampling to consume `*_variance`/`*_std` fields directly. `epistemic_variance` is available for active learning.
