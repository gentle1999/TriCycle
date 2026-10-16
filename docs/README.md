# Project documentation

This directory maintains architecture, plugin interfaces, data and experimental protocols, operations, and recovery procedures. Individual run status belongs in local run records; formal results are archived separately. Maintained documentation and agent-authored records use English by default, including when user instructions are in Chinese, unless the user explicitly requests another language.

## Sampling and model experiments

- [Sampling replay entry points](intelligent-replay.md): choose the round-based suite, full-trajectory workers, or local single-batch selection.
- [Unified sampling experiments](mae-intelligent-sampling.md): shared conditions, five baselines and intelligent sampling, budget evaluation, execution, and recovery.
- [Sampling baseline methods and sources](sampling-baselines.md): five distinct primary references, evidence of use, and TriCycle adaptation boundaries.
- [Sampling package interface](../packages/tricycle-sampling/README.md): request fields, variance conversions, KB adapters, and audit records.
- [Frozen sampling spaces](frozen-sampling-space.md) and [dataset adaptation](reaction-dataset-adaptation.md).
- [SAR model benchmarks](model-baselines.md), the [SAR package](../packages/tricycle-sar/README.md), and [SAR accelerator execution](sar-accelerator-execution.md).

## Architecture and integration

- [Architecture and design boundaries](architecture.md).
- [Plugin contract](plugin-contract.md) and [development](development.md).
- [Run record scope and output destinations](run-records.md).
- [Mounted run storage with a local cache](mounted-run-storage.md).
- [Live replay containers, snapshots, and recovery](compact-replay-records.md).
- [Bohr model environments](bohr-dl.md).
- [Reaction database integration](reaction-db.md).
- [Balance alerts](balance-alert.md).
- [Blocked trajectory email alerts](trajectory-alerts.md).

## Research and run archives

Default runs use `runs/YYYY-MM-DD/HH-MM-SS.ffffff±ZZZZ/<run-id>` with dates and times in the host's local timezone. Explicit directories and recovery paths are preserved. Run state, credential-related material, machine paths, and supervision logs do not belong in general guides.

Local research reports, formal result packages, figures, and report reproduction records belong in Git-ignored `reports/`; reusable assets and independent audits belong in `artifacts/`. Apply the [run admission rule](run-records.md) before writing execution records. Move obsolete documentation byte for byte to `artifacts/document-archive/`, recording its original path, hash, and replacement entry point. Maintained guides and tests must not depend on local archives.

SAR five-fold methods combine shared environment setup and training/inference entry points through [backends/catalog.json](../packages/tricycle-sar/src/tricycle_sar/backends/catalog.json); the [lock file](../configs/sar-training-protocols.lock.json) references their hash bindings. Maintain only current methods and source. Scientific result verification is independent of execution source, and reruns use current backends in new directories.

Offline statistics, SAR and sampling figures, and their dependency lock live in [`analysis/`](../analysis/README.md). Model packages provide execution and result-verification interfaces consumed by the analysis layer; `packages/` must not import that layer.

- [Reproducible result packages](reproducible-results.md): portable observations,
  independent verification, language comparisons, and scoped method identities.
