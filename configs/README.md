# Current configuration

This directory maintains current request examples, experimental protocols, and declarative bindings. Run receipts, one-off scheduling settings, and superseded experiment configurations belong in local archives outside source commits.

| Purpose | Configuration |
| --- | --- |
| Capability requests | `example-*.toml`; fill in inputs, paths, and resources for the current task |
| Single-batch candidate selection | `sampling-baselines/*.toml` |
| Round-by-round comparison of five baselines and intelligent sampling | `dpa-unified-sampling-suite.toml` |
| Full-trajectory workers for the five baselines | `rxnresid-matched-sampling.toml` |
| Four-pool database export | `database-sampling-pools.toml` |
| Current frozen data space | `frozen-sampling-space.toml`; maintain this single current pin |
| SAR five-fold benchmarks and current backend references | `example-sar-benchmark.toml`, `sar-training-protocols.lock.json` |
| SAR scheduling and read-only image inventory | `example-sar-bohr-group.toml`, `example-sar-inventory.toml` |
| Reporting subagent and retry policy for blocked-trajectory email | `example-trajectory-alerts.toml`; SMTP routing and credentials use `.env.example` variables |

Bohr GPU requests and GPU environments in the SAR catalog use the unified machine type. Pure Gaussian and fingerprint models use CPUs; RXNGraphormer uses the internal executor. Concrete machine types belong only in requests and the catalog. Do not set a global GPU default that overrides all computation.

Sampling configurations retain their own readiness, data, and cost-ledger bindings; they are not general data defaults. Verify inputs before preparing a new experiment's independent configuration with the relevant skill. Never automatically rebind historical trajectories to the current space. SAR preparation requires an explicit `--group-config` and does not inherit a particular smoke run's job group or authorization state.

Archive obsolete configurations byte for byte under Git-ignored `artifacts/config-archive/<timestamp>/`. Record original paths, SHA-256 hashes, reasons, and replacement entry points in the manifest. Relative paths in archived configurations retain their original interpretation; restore the corresponding layout in an isolated directory when verifying historical configurations. Current code, tests, and operating guides must not depend on archives. Keep original data snapshots, job records, and checkpoints in their run directories.

`codex-usage-alert.toml` and `decision-model-balance-alert.draft.toml` are existing machine-local alert configurations and remain excluded by `.gitignore`.
`trajectory-alerts.toml` is also a Git-ignored deployment configuration. See
[trajectory alerts](../docs/trajectory-alerts.md) before enabling delivery.
