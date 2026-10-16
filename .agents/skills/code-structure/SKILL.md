---
name: code-structure
description: "Use when adding, changing, splitting, moving, or reviewing TriCycle code. Define module ownership, dependencies, CLI and worker boundaries, runtime assets, and validation. Applies to structural refactoring and interface changes, not merely running existing experiments or editing experimental data."
---

# TriCycle Code Structure

Place each capability in the package responsible for its domain, and make callers depend on explicit interfaces. Follow the user's current intent and repository [AGENTS.md](../../../AGENTS.md). This skill does not expand task scope or add approval requirements for routine local refactoring. Work in English, including delegated analysis, decisions, and records, unless the user explicitly requests otherwise; Chinese input alone does not change that default.

## Before editing

1. Inspect the working tree and target modules; preserve existing changes. Read the owning package's README, callers, and tests. Search imports, module names, script paths, and configuration references with `rg`; filenames alone do not establish responsibility.
2. State what the logic does, which module should own it, and who calls it. Split scripts with mixed responsibilities; moving an entire `tools` file into a package is not sufficient by itself.
3. Find existing interfaces and implementations first. Use existing modules for local fixes, and add a module only for a distinct responsibility. Do not introduce generic frameworks, factories, adapters, or `utils` collections without actual reuse needs.

## Separate guidance, procedures, and configuration

- Keep global engineering conventions and skill entry points in `AGENTS.md`, not module-specific models, environments, commands, or experiment details. Package READMEs or module documentation own interfaces and implementation constraints.
- Skills describe when to act, which configuration to read, required fields, validation, and failure handling. Do not embed source commits, checksums, or image digests, or make them conditions for using a skill.
- Owning configuration or asset manifests supply expected revisions, checksums, and environment bindings. Code reads and validates them; run records capture actual values. Skills may link configuration and explain fields without becoming a second configuration source.
- Binding-value changes alone do not require skill edits. Update guidance when configuration fields, interfaces, or procedures change. Documentation examples should reference configuration instead of duplicating values that can become stale.
- `configs/` maintains current configuration and usable examples only. Archive replaced experiment bindings byte for byte under Git-ignored `artifacts/config-archive/`, recording original paths, hashes, and replacements. Remove stale references from defaults, tests, and procedures; current execution must not depend on local archives. Do not rewrite existing run records or data.
- Documentation maintains current interfaces and reusable procedures, not individual experiment scores, completion status, task IDs, or one-off audit counts. Follow [run record scope](../../../docs/run-records.md): execution/recovery evidence belongs in `runs/`, offline result packages and figures in `reports/`, reusable assets and independent audits in `artifacts/`, host-service state in `.tricycle/`, and disposable validation in temporary directories. Do not add ignore exceptions for individual experiments.
- Archive obsolete documents byte for byte in `artifacts/document-archive/`, with original paths, hashes, and replacements, before removing them from maintained directories and Git tracking. For mixed guides and experiment records, preserve the original first, then extract reusable guidance. Tests use small synthetic data to validate interfaces, not local experiment reports.

## Directory ownership

Package names below refer to subprojects under `packages/`; runtime code lives in `src/<package_name>/`.

| Location | Responsibility |
| --- | --- |
| `tricycle-core` | Shared types, capability contracts, run records, and common infrastructure; no specific models or domain strategies |
| `tricycle-bohr` | Single-job platform protocols and shared request/execution infrastructure for model tasks |
| `tricycle-bohrsubmit` | Batch preparation, input packaging, job groups, submission/status/download/recovery, and concrete Bohr CLI adapters |
| `tricycle-sar` | SAR backends, environment initialization, training/inference, task assembly, runtime diagnostics, predictions, and result validation |
| `tricycle-rits` | RitS training/inference and task preparation/execution integration |
| `tricycle-sampling` | Candidate selection, acquisition functions, sampling metrics, active learning, replay, round decisions, supervised recovery, space snapshots, and workers |
| `tricycle-reaction-db` | Database protocols/adapters, exports, and reaction/substrate identity adaptation; structural matching is not plotting logic |
| `tricycle-reactionopt` | Reaction configuration, candidate enumeration, conformers, and reaction profiles |
| `tricycle-exploration` | Exploration-round contracts, evidence records, candidate-space decisions, and training gates |
| `apps/tricycle-agent` | Capability registration, dependency injection, CLI dispatch, and cross-capability campaign orchestration; domain algorithms stay in domain packages |
| `analysis/` | Offline statistics, scientific plotting, report generation, and their independent dependency environment |
| `tools/` | Standalone development, CI, operations diagnostics, and example orchestration; may consume package APIs, but is not a runtime library or worker source repository |
| `configs/` | Requests, examples, and declarative bindings; no parallel algorithms, initialization, or execution implementation |
| `runs/` | Task-owned execution, verification, and recovery records under an existing task/campaign manifest |
| `reports/` | Offline comparisons, figures, and published result packages referencing source evidence |
| `artifacts/`, `.tricycle/` | Reusable assets/independent audits and host-service state, respectively; neither is an alternative source tree |
| `.agents/skills/` | The sole project skill directory for domain procedures, capability operations, and development conventions; merge duplicate names without parallel directories or forwarding files |

Training losses, statistics that affect next-round selection, and scientific validity checks are runtime logic. Distribution statistics, curves, and plotting used only for retrospective explanation belong in `analysis/`. Split mixed scripts and let analysis consume package results.

## Dependencies and entry points

- Applications call domain packages; domain packages call the shared foundation. Explicit, declared dependencies between domain packages are allowed if acyclic. `core` must not depend on plugins.
- `packages/` must not import `apps`, `tools`, or `analysis`, or bypass this boundary through file reads, source copies, dynamic loading, subprocesses, or string paths. Do not launch the `tricycle-agent` CLI from a package to reuse domain behavior; call package APIs and inject adapters.
- Ship required runtime source and assets with the package. Do not traverse back to the checkout with `__file__.parents[n]`, modify `sys.path`, or otherwise locate `tools/` or external source implicitly. Caller-supplied data, request, and configuration paths may be outside packages; default locations need explicit contracts independent of source-tree depth.
- CLIs parse arguments, bind requests, call APIs, and present results. Libraries reuse APIs rather than parse another CLI's output. Declared console entry points, package `__main__`, and public API exports may remain.
- Keep only canonical request fields, strategy names, and operation names. Update callers and examples during migration; reject obsolete and unknown fields instead of ignoring them or resolving competing aliases by precedence. Serialized requests intended for replay must parse under the same contract. Platform jobs, application run directories, and domain requests each follow their own contracts, without fallback aliases that mix them.
- Update callers directly and remove old scripts and adjacent `.pyi` files after migration. Do not retain import-and-call Python wrappers, shell redirectors, `sys.modules` aliases, or compatibility shells for old paths.
- Remote workers may generate launch scripts for working directories, environment activation, logging, exit status, and process startup. Implementations still come from current package modules. Do not duplicate launch or domain logic per model, fold, or seed.
- Inject external services through protocols/adapters. Declare dependencies in the package or extra that actually uses them. Default planning and dry-run paths read no credentials, access no network, and submit no jobs.

## Current implementation and runtime assets

- Maintain one current project implementation. Routine fixes must not add `legacy` trees, historical catalogs, old source snapshots, or versioned compatibility branches. Upload directories are not a second maintained protocol implementation.
- Data format versions, pinned third-party weights, immutable data/splits, existing run records, and checkpoints are not historical execution implementations. Structural cleanup must not delete or rewrite experimental facts. Reruns create new runs bound to the actual current method.
- Tasks reuse their owning package's preparation, execution, and result interfaces and select current implementations through configuration. Record shared data, splits, environments, and configuration once; repeated tasks record only differences.
- Generating delivery bundles from current verified packaged source and assets is necessary staging, not a persistent source fork. Inspect actual delivered contents; a local checkout must not mask missing modules.
- For intentional changes to hash-checked source, inspect the diff, update affected configuration or asset manifests, and validate. Do not copy new hashes into skills. Investigate unexplained hash drift instead of automatically refreshing hashes, disabling checks, or rewriting historical manifests to make checks pass.
- Directory refactoring preserves the selected configuration's environment and dependency semantics. Read asset, executor, and result-retention rules from owning module documentation and configuration; do not add model-specific constraints to this general structural skill.

## Migration and completion

1. Move logic to its owning module while preserving explicit requests, results, and error behavior. Expose only cross-package interfaces actually needed. Do not move model, database, or sampling business logic into `core` to solve imports.
2. Update direct imports, dynamic module names, subprocess commands, staging inventories, configuration, examples, documentation, and skills together. Interface changes also require `.pyi`, dependency, packaged-resource, and relevant test updates.
3. Remove obsolete entry points and duplicate implementations, then search for old names and paths. Historical run paths are original evidence, not source references to update. Current executable call chains and procedures must not reference deleted files.
4. Preserve submission, failure-recovery, and retry semantics. Network retries belong to platform adapters; environment-setup retries belong to shared initializers. Avoid layered retry amplification and unsupported resubmission when the original submission outcome is unknown.
5. Validate affected behavior using the guidance below. Report ownership, removed entry points, actual checks, and unverified scope. Distinguish successful preparation, environment smoke checks, and full training/inference.

## Validation selection

Prefer existing checks; run commands from the repository root.

| Change | Relevant checks |
| --- | --- |
| Directory moves and package dependencies | [Runtime independence from tools](../../../packages/tricycle-sampling/tests/test_sampling_runtime_boundary.py), [runtime/analysis boundaries](../../../tests/analysis/test_runtime_boundary.py) |
| Worker packaging, resource lookup, preparation entry points | [Delivery without checkout access](../../../packages/tricycle-sar/tests/test_smoke_staging.py) and affected offline preparation smoke checks |
| Function interfaces and stubs | [Source/`.pyi` consistency](../../../tests/test_type_interfaces.py), owning package types, and caller behavior |
| Current SAR backends and integrity bindings | `uv run --all-packages python -m tricycle_sar validate`, followed by targeted checks for affected backends |
| Cross-package runtime logic | Relevant regressions; use `uv run --all-packages pytest`, `uv run ruff check .`, and `make typecheck` according to impact |

A structural migration can start with:

```bash
uv run --all-packages pytest \
  packages/tricycle-sampling/tests/test_sampling_runtime_boundary.py \
  tests/analysis/test_runtime_boundary.py \
  packages/tricycle-sar/tests/test_smoke_staging.py \
  tests/test_type_interfaces.py
git diff --check
```

Static scans cover only some syntax. Inspect dynamic paths, subprocesses, and staging too. Validate delivery in a directory without repository-source fallback, providing only declared dependencies and resources. Standard-library-only entry points can use `python -I -S`; third-party entry points need an isolated environment with explicitly installed dependencies.

Add tests for new behavioral risks, not fixed file counts or assertions that merely copy implementation details to freeze a directory. Documentation, skills, and low-impact reversible changes need link, format, and convention checks rather than model training. Live Bohr smoke checks must stay within existing user authorization. This skill does not itself authorize cloud submission, task termination, or host-service changes.

## Decision examples

| Request or finding | Action |
| --- | --- |
| Replay worker copies `tools/foo.py` | Move implementation into sampling, stage resources from the delivered package, remove the old entry point, and validate isolated execution |
| One script exports database data, matches identities, and plots distributions | Put export/adaptation in reaction-db and figures in analysis; connect them through package APIs |
| A new model reuses an initializer with different parameters and runner | Compose the current catalog without copied directories or per-model forwarding scripts |
| A file move is proposed to require a frozen v2 or old-command compatibility | Update the current implementation and callers, preserving existing run records without another project-source version |
| Worker needs environment setup, logging, and training | Keep a process-lifecycle launcher that reuses packaged initialization and training |
