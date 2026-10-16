# TriCycle Project Skills

`.agents/skills/` is the sole project skill directory for capability operations, domain workflows, and development conventions. Each name has one `SKILL.md`; do not maintain parallel directories, duplicate names, or forwarding skills. Package READMEs define module interfaces. Read concrete revisions, hashes, and image bindings from configuration or asset manifests instead of copying them into skills.

Use English for skill instructions, task execution, decisions, and records, including delegated work and tasks requested in Chinese, unless the user explicitly requests another language. Pass this requirement and any explicit language exception to subagents. See [AGENTS.md](../../AGENTS.md) for global collaboration rules.

## Capability operations

Before writing generated output, apply [run record scope](../../docs/run-records.md). A task's execution and recovery evidence belongs in its existing run; standalone reports, reusable assets, service state, and disposable checks use their designated destinations. Do not create a new run merely for an inspection or a development note.

These skills correspond to capabilities registered in the application catalog. Default planning does not connect to external services; each skill defines its authorization and recovery boundaries.

| Skill | Capability |
| --- | --- |
| [reactionopt-generate](reactionopt-generate/SKILL.md) | `reactionopt.generate` |
| [reactionopt-enumerate-da](reactionopt-enumerate-da/SKILL.md) | `reactionopt.enumerate-da` |
| [reactionopt-export](reactionopt-export/SKILL.md) | `reactionopt.export` |
| [reactionopt-profile](reactionopt-profile/SKILL.md) | `reactionopt.profile` |
| [candidate-select](candidate-select/SKILL.md) | `candidate.select` |
| [exploration-round](exploration-round/SKILL.md) | `exploration.round` |
| [bohr-compute-plan](bohr-compute-plan/SKILL.md) | `bohr.compute.plan` |
| [bohr-compute-submit](bohr-compute-submit/SKILL.md) | `bohr.compute.submit` |
| [bohr-job-status](bohr-job-status/SKILL.md) | `bohr.job.status` |
| [bohr-job-download](bohr-job-download/SKILL.md) | `bohr.job.download` |
| [bohr-batch-prepare](bohr-batch-prepare/SKILL.md) | `bohr.batch.prepare` |
| [bohr-batch-submit](bohr-batch-submit/SKILL.md) | `bohr.batch.submit` |
| [bohr-batch-status](bohr-batch-status/SKILL.md) | `bohr.batch.status` |
| [bohr-batch-download](bohr-batch-download/SKILL.md) | `bohr.batch.download` |
| [bohr-batch-watch](bohr-batch-watch/SKILL.md) | `bohr.batch.watch` |
| [bohr-batch-terminate](bohr-batch-terminate/SKILL.md) | `bohr.batch.terminate` |
| [dpa-berny-prepare](dpa-berny-prepare/SKILL.md) | `dpa-berny.prepare` |
| [dpa-berny-rescue](dpa-berny-rescue/SKILL.md) | `dpa-berny.rescue` |
| [reaction-db-ingest](reaction-db-ingest/SKILL.md) | `reaction-db.ingest` |
| [reaction-db-batch-import](reaction-db-batch-import/SKILL.md) | `reaction-db.batch-import` |
| [reaction-db-filter](reaction-db-filter/SKILL.md) | `reaction-db.filter` |
| [reaction-db-export-rits](reaction-db-export-rits/SKILL.md) | `reaction-db.export-rits` |
| [reaction-db-export-rxnresid](reaction-db-export-rxnresid/SKILL.md) | `reaction-db.export-rxnresid` |
| [sar-train](sar-train/SKILL.md) | `sar.train` |
| [sar-infer](sar-infer/SKILL.md) | `sar.infer` |
| [sar-finetune](sar-finetune/SKILL.md) | `sar.finetune` |
| [rits-train](rits-train/SKILL.md) | `rits.train` |
| [rits-infer](rits-infer/SKILL.md) | `rits.infer` |
| [rits-finetune](rits-finetune/SKILL.md) | `rits.finetune` |
| [rits-ssh-finetune](rits-ssh-finetune/SKILL.md) | `rits.ssh.finetune` |
| [rits-ssh-status](rits-ssh-status/SKILL.md) | `rits.ssh.status` |
| [rits-ssh-logs](rits-ssh-logs/SKILL.md) | `rits.ssh.logs` |
| [rits-ssh-terminate](rits-ssh-terminate/SKILL.md) | `rits.ssh.terminate` |
| [rits-ssh-download](rits-ssh-download/SKILL.md) | `rits.ssh.download` |

## Domain and composite workflows

| Skill | Purpose |
| --- | --- |
| [bohr-compute](bohr-compute/SKILL.md) | Single-job calculation planning, submission, and result lifecycle |
| [bohr-submit](bohr-submit/SKILL.md) | Batch staging, calculation inputs, and lifecycle |
| [reactionopt-config](reactionopt-config/SKILL.md) | Reaction configuration generation and downstream handoff |
| [rits-model](rits-model/SKILL.md) | RitS training, fine-tuning, inference, and executor selection |
| [sar-model](sar-model/SKILL.md) | SAR backends, task preparation, and result management |
| [sar-benchmark](sar-benchmark/SKILL.md) | Grouped cross-validation, model comparisons, and publication |
| [sampling-strategy](sampling-strategy/SKILL.md) | Strategy selection, matched experiments, and sampling boundaries |
| [agent-authored-acquisition](agent-authored-acquisition/SKILL.md) | Author and validate per-round acquisition functions under the selected protocol |
| [exploration-campaign](exploration-campaign/SKILL.md) | Supervision and recovery of persistent QM exploration or historical-label replay |

## Development conventions

| Skill | Purpose |
| --- | --- |
| [code-structure](code-structure/SKILL.md) | Directory responsibilities, dependencies, configuration ownership, and migration validation |
| [commit-log](commit-log/SKILL.md) | English Conventional Commits messages and commit preparation |

Capability requests and adapter policy use TOML. Native model inputs, replay state, and result files follow their owning modules' data contracts. Command output is not directly reusable as request configuration.
