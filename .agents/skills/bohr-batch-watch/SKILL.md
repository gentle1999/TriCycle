---
name: bohr-batch-watch
description: "Poll a prepared Bohr batch run through the adapter until completion or a configured limit."
---

# 观察 Bohr 批量运行

输入是已提交的 `run.toml`/批量报告以及轮询参数；输出是持续更新的 `status.tsv` 和运行状态。
它只读远程状态，不适用于首次提交或终止任务。

仅对已经提交的 run 使用。设置合理的轮询间隔和最大迭代次数；长时间运行应把状态写入 `run.toml`，
而不是依赖当前会话。默认 dry-run 不轮询远程平台，真实观察需要 `--live` 和 Bohr 环境变量（可由项目 `.env` 自动加载）。

如果只需单次查询，使用 `bohr.batch.status`；任务完成后再使用下载能力。

## 失败与恢复

达到 `max_iterations` 或发生临时网关错误时，先 `inspect` 运行状态，再用 `resume --live`
继续观察；需要调整轮询参数时修改原始 TOML 后创建新的运行实例。
