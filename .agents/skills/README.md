# TriCycle Codex Skills

`.agents/skills/` 是仓库级 Skill 的发现入口。每个目录对应一个稳定的
capability ID；Skill 只描述适用场景、TOML 请求和安全边界，实际实现由
`tricycle-agent` 的能力目录分发。

| Skill | Capability |
| --- | --- |
| `reactionopt-generate` | `reactionopt.generate` |
| `reactionopt-export` | `reactionopt.export` |
| `candidate-select` | `candidate.select` |
| `bohr-compute-plan` | `bohr.compute.plan` |
| `bohr-compute-submit` | `bohr.compute.submit` |
| `bohr-batch-prepare` | `bohr.batch.prepare` |
| `bohr-batch-submit` | `bohr.batch.submit` |
| `bohr-batch-status` / `bohr-batch-watch` | `bohr.batch.status` / `bohr.batch.watch` |
| `bohr-batch-terminate` | `bohr.batch.terminate` |
| `bohr-batch-download` | `bohr.batch.download` |
| `bohr-job-status` | `bohr.job.status` |
| `bohr-job-download` | `bohr.job.download` |
| `dpa-berny-prepare` | `dpa-berny.prepare` |
| `dpa-berny-rescue` | `dpa-berny.rescue` |
| `reaction-db-ingest` | `reaction-db.ingest` |
| `sar-train` / `sar-finetune` / `sar-infer` | `sar.train` / `sar.finetune` / `sar.infer` |
| `rits-train` / `rits-finetune` / `rits-infer` | `rits.train` / `rits.finetune` / `rits.infer` |

用户请求、adapter 策略和运行状态全部使用 TOML；命令输出可以使用 JSON，
但输出 JSON 不是配置文件。
