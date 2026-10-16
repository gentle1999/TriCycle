# TriCycle Agent

TriCycle is a Python workspace for exploring cycloaddition reactions. It connects candidate generation, sampling, Bohr calculations, reaction databases, and SAR/RitS model tasks. The application registers capabilities and injects external adapters; each domain package owns its implementation.

English is the project's primary working language for documentation, skills, code-facing explanations, agent decisions, and records. Instructions in another language still lead to English execution and reporting unless the user explicitly requests that language. Delegated agents follow the same policy. See [AGENTS.md](AGENTS.md) for the full collaboration rules.

## Installation and local checks

Python `>=3.11` and `uv` are required. Run from the repository root:

```bash
uv sync --all-packages
uv run --all-packages tricycle-agent capabilities
uv run --all-packages tricycle-agent plan \
  rits.infer configs/example-rits-infer-request.toml --run-id rits-plan-demo
```

`plan` validates the request and produces a plan. Fill in the example's input paths, project, and resources for the actual task. A successful plan does not establish input readiness or model acceptance.

```bash
make check
make ci-test
make typecheck
```

`make check` provides basic checks without uv. `make ci-test` runs the full dependency environment and subproject tests. See [development](docs/development.md) for coverage.

## Packages and entry points

| Subproject | Responsibility and main entry points |
| --- | --- |
| [tricycle-core](packages/tricycle-core/README.md) | Shared types, protocols, capability catalog, run state, traces, file and metric utilities |
| [tricycle-bohr](packages/tricycle-bohr/README.md) | Single calculation and model job contracts, planning, and injected execution boundaries |
| [tricycle-bohrsubmit](packages/tricycle-bohrsubmit/README.md) | Batch staging, job groups, submission/status/download/recovery, and DPA-Berny/Gaussian |
| [tricycle-reaction-db](packages/tricycle-reaction-db/README.md) | Calculation-log import, database filtering, training-data export, and substrate identity adaptation |
| [tricycle-sar](packages/tricycle-sar/README.md) | Model backends, environment setup, training/inference, benchmarks, and result verification; `sar.*` capabilities target RxnResid |
| [tricycle-rits](packages/tricycle-rits/README.md) | RitS training, fine-tuning, inference, and SSH/Docker tasks |
| [tricycle-sampling](packages/tricycle-sampling/README.md) | Candidate selection, literature baselines, intelligent acquisition, replay, and supervision |
| [tricycle-reactionopt](packages/tricycle-reactionopt/README.md) | Reaction configuration, DA enumeration, and independent reactant/product/RitS TS profile branches |
| [tricycle-exploration](packages/tricycle-exploration/README.md) | Exploration-round records, search-space decisions, RitS train/skip gates, and snapshot transitions |
| [tricycle-agent](apps/tricycle-agent/README.md) | Capability registration, CLI, adapter assembly, and persistent exploration campaigns |

Domain logic lives in `packages/`. `tools/` contains development, operations, and example orchestration only. Offline analysis, plotting, and their environment live in [analysis/](analysis/README.md). The sole skill directory is [.agents/skills/](.agents/skills/README.md). See [architecture](docs/architecture.md) for dependency directions and execution boundaries.

## Configuration and computation

[configs/](configs/README.md) maintains current requests, experiment bindings, and examples. Source revisions, weight checksums, images, and machine types come from configuration or asset manifests; documentation does not duplicate those bindings.

- SAR uses the [backend catalog](packages/tricycle-sar/src/tricycle_sar/backends/catalog.json) and its environment configurations. RxnResid source and entry points are also specified in its [model assets](packages/tricycle-sar/src/tricycle_sar/assets/rxnresid-model.toml).
- RitS source and weights are specified in its [model assets](packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml); Bohr requests are in `configs/example-rits-*-request.toml`.
- Bohr GPU resources use the unified machine type selected by current configuration. DPA requires GPU execution; pure Gaussian and fingerprint models use CPUs. RXNGraphormer uses the configured internal executor. See [Bohr model environments](docs/bohr-dl.md) for environment, device, and failure checks.

Generic capability requests and `run.toml` use TOML. Native model YAML/JSON, the SAR catalog, result manifests, and sampling-controller JSON state retain their own contracts.

Remote Bohr operations require an installed and configured `bohr` CLI. Adapter policy is described by [example-bohr.toml](configs/example-bohr.toml). Inject credentials through the process environment or a Git-ignored `.env`:

```bash
cp .env.example .env
```

Edit the configuration for the local host and never commit secrets. Common variables include `BOHR_ACCESS_KEY`, `BOHR_BIN`, `BOHR_PROJECT_ID`, `BOHR_IMAGE`, and `BOHR_ML_IMAGE`. Explicit request values override environment defaults. Do not use a global `BOHR_MACHINE_TYPE` to turn CPU tasks into GPU tasks. Default dry runs neither construct live adapters nor access external services.

## Execution and recovery

```bash
# Select candidates locally and write this run's records.
uv run --all-packages tricycle-agent run \
  candidate.select configs/example-sampling-request.toml --run-id sampling-demo

# Inspect local records; resume the next action recorded in execution state.
uv run --all-packages tricycle-agent inspect --run-dir /absolute/run
uv run --all-packages tricycle-agent resume --run-dir /absolute/run --live
```

Use `--live` only for authorized external operations. Destructive capabilities also require `--confirm`. Resume existing runs instead of initializing them again. When submission receipts are uncertain, reconcile platform records before proceeding to prevent duplicate jobs. See [BohrSubmit](packages/tricycle-bohrsubmit/README.md) for batch workflows and downloading partial artifacts from failed workers.

`tricycle-agent campaign` supervises persistent exploration, starting from an existing Bohr batch and coordinating monitoring, downloads, analysis, selection, and preparation of the next batch. See the [exploration-campaign skill](.agents/skills/exploration-campaign/SKILL.md) for inputs and recovery. The two sampling-replay execution entry points are described in the [sampling guide](docs/intelligent-replay.md).

## Documentation and run artifacts

The [documentation index](docs/README.md) covers interfaces, protocols, operations, and development. Default run directories follow `runs/YYYY-MM-DD/HH-MM-SS.ffffff±ZZZZ/<run-id>` in the host's local timezone. Explicit directories and recovery paths are preserved as requested.

Follow [run record scope](docs/run-records.md): `runs/` contains task-owned execution and recovery evidence; `reports/` contains offline comparisons, figures, and published result packages; `artifacts/` contains reusable assets and independent audits. Host-service state belongs in `.tricycle/`, and disposable checks use temporary directories. These outputs stay outside source commits. Archive obsolete documentation byte for byte in `artifacts/document-archive/`, recording its original path and hash, before removing it from maintained documentation and Git tracking. Current guides must not depend on local archives; tests use small synthetic data rather than a particular experiment's results.

Read [AGENTS.md](AGENTS.md) and the [code-structure skill](.agents/skills/code-structure/SKILL.md) before development. New capabilities require synchronized package READMEs, the [plugin contract](docs/plugin-contract.md), skills, and request examples.
