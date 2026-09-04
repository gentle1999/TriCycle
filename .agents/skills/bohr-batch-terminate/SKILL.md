---
name: bohr-batch-terminate
description: "Terminate selected incomplete Bohr batch workers only after explicit destructive-action confirmation."
---

# 终止 Bohr 批量任务

输入是已有批量运行目录和明确的 worker 选择；输出为终止报告及更新后的 `run.toml`。这是唯一的
destructive 批量能力，不适用于普通状态查询或结果下载。

这是 destructive 能力。先查询状态并明确列出要终止的 worker；默认 dry-run 只生成计划。真实执行必须同时满足：

- 使用 `--live`；
- 使用 `--confirm`；
- 远程凭据通过环境变量或项目 `.env` 注入。

终止结果和公开 ID 写入 `run.toml`/报告。没有明确确认时不要调用该能力。

## 失败与恢复

终止失败时运行状态可能保留同一下一动作，但不会在无确认时自动执行。先检查报告和远程状态，
重新确认目标 worker 后，再发起一次同时带 `--live --confirm` 的调用；不得仅凭聊天记录重试。
