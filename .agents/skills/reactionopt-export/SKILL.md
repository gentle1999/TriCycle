---
name: reactionopt-export
description: "Export a generated ReactionOpt configuration through an explicitly injected local adapter."
---

# 导出 ReactionOpt 配置

仅当已有明确的 `ReactionOptAdapter` 和导出目标时使用。请求仍使用 TOML；默认 dry-run 只生成计划，
不会调用实际 ReactionOpt 项目。真实导出必须由应用注入 adapter，并把输出位置作为 artifact 记录到
`run.toml`，不得直接 import 外部项目或把路径/凭据硬编码进 Skill。

## 失败与恢复

adapter 失败时先用 `inspect` 查看错误和输出位置，修复 adapter 后使用新的 `run-id` 和明确的
输出目标重新执行；当前 adapter 契约不保证覆盖已有输出，因此不要盲目 `resume` 或自动猜测
ReactionOpt 参数或覆盖已有输出目录。

## 不适用

没有明确的 `ReactionOptAdapter` 或导出目标时只使用 `reactionopt.generate`；本 Skill 不负责生成
反应参数，也不负责提交 Bohr 任务。
