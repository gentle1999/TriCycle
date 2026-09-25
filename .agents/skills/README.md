# TriCycle Codex Skills

`.agents/skills/` 是仓库级 Skill 的发现入口。目录对应稳定 capability ID 或组合工作流；Skill 只描述适用场景、TOML 请求和安全边界，实际实现由
`tricycle-agent` 的能力目录分发。

| Skill | Capability |
| --- | --- |
| `reactionopt-generate` | `reactionopt.generate` |
| `reactionopt-export` | `reactionopt.export` |
| `reactionopt-profile` | `reactionopt.profile` |
| `candidate-select` | `candidate.select` |
| `agent-authored-acquisition` | `exploration.round`（要求上一批结果已下载；运行 RDKit 结构分析并记录证据后本地选择） |
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
| `rits-ssh-finetune` / `rits-ssh-status` / `rits-ssh-logs` | `rits.ssh.finetune` / `rits.ssh.status` / `rits.ssh.logs` |
| `rits-ssh-terminate` / `rits-ssh-download` | `rits.ssh.terminate` / `rits.ssh.download` |

用户请求、adapter 策略和运行状态全部使用 TOML；命令输出可以使用 JSON，
但输出 JSON 不是配置文件。

## 开发辅助 Skill

| Skill | 用途 |
| --- | --- |
| [`commit-log`](commit-log/SKILL.md) | 按项目惯例撰写和审阅 Git 提交日志 |
