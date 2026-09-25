---
name: bohr-batch-status
description: "Query all submitted workers in a Bohr batch run and persist a status report in TOML-backed run state."
---

# 查询 Bohr 批量状态

## 输入与边界

输入是已有 `run.toml`、`submit-results.tsv` 和远程公开 `BOHR_ID` 的运行目录；输出是
`status.tsv` 和更新后的 `run.toml`。本能力只读远程状态，不提交、下载或终止 worker。

新版 CLI 的 `id` 是平台内部 ID，不能直接传给生命周期命令。兼容旧批次时，若运行目录
包含 `job_group_id`，adapter 会调用 `bohr job describe --job_group_id` 将旧 `id` 映射为
`bohrId`；新提交无需额外处理。

需要已有 `submit-results.tsv` 和远程 job ID。推荐从状态文件恢复：

```bash
uv run --package tricycle-agent tricycle-agent resume \
  --run-dir 'runs/<date>/<timestamp>/<id>' --live
```

真实查询产生 `status.tsv`，默认 dry-run 不调用网络；凭据可放在项目 `.env` 中，shell 环境变量优先。查询后检查 `incomplete`、`failed` 和下一动作，
不要把 TSV 报告误当成配置文件。

## 失败与恢复

该能力只读远程状态，不提交或终止 worker。临时网络、凭据或单 worker 查询错误时保留
`status.tsv`，修复环境后用 `resume --live` 重试；状态文件会保留下一动作。
