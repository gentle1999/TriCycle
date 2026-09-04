---
name: bohr-job-status
description: "Read the status of an existing Bohr job or prepared batch run and persist the next action."
---

# 查询 Bohr 任务状态

## 适用场景

当 run 已记录新版 CLI 的公开 `BOHR_ID` 或 job group ID，需要判断是否仍在运行、完成或失败时使用。
不要把 `bohr job list` 输出中的平台内部 `id` 当作 job ID；旧批次需同时提供任务组上下文，adapter 才能将其映射为 `bohrId`。

## 执行

批量 run 使用：

```bash
uv run --package tricycle-agent tricycle-agent resume \
  --run-dir runs/<id> --live
```

单任务请求 TOML 至少包含 `external_id`，能力为 `bohr.job.status`。默认 dry-run 不发起网络请求；
真实查询需要 `--live` 和 Bohr 环境变量（工具会自动发现项目 `.env`）。查询结果写入状态/报告，完成后 `next_action` 通常指向下载。

## 失败与恢复

临时查询失败时先 `inspect` 保留的错误状态，修复环境后用 `resume --live` 重试；远程终态
应停止轮询，并按 `next_action` 转入下载或人工处理。
