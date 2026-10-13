---
name: bohr-batch-watch
description: "Poll a prepared Bohr batch run through the adapter until completion or a configured limit."
---

# 观察 Bohr 批量运行

输入是已提交的 `run.toml`/批量报告以及轮询参数；输出是持续更新的 `status.tsv` 和运行状态。
它查询远程状态；请求设置 `download_when_complete = true` 时，还会在完成后下载到本地。它不提交或终止任务。

仅对已经提交的 run 使用。设置合理的轮询间隔和最大迭代次数；长时间运行应把状态写入 `run.toml`，
而不是依赖当前会话。默认 dry-run 不轮询远程平台，真实观察需要 `--live` 和 Bohr 环境变量（可由项目 `.env` 自动加载）。

如果只需单次查询，使用 `bohr.batch.status`；任务完成后再使用下载能力。

## 失败与恢复

达到 `max_iterations` 或发生临时网关错误时，先 inspect；仅在 `resume.next_action = "bohr.batch.watch"` 时用 `resume --live` 继续观察。完成后下一动作可能为下载，不能继续把 resume 当作 watch。
调整轮询参数时，通过注入 gateway 的 `BohrSubmitPlugin.watch` 显式传入新值；已有 run 的请求不可变，不修改原始 TOML 或用 `run` 重建同一目录。
