---
name: bohr-job-download
description: "Download completed Bohr artifacts into a run directory through the configured adapter."
---

# 下载 Bohr 结果

## 适用场景

仅在状态确认任务完成或用户明确要求下载时使用。

## 执行

单任务 TOML 至少包含公开 `external_id`（新版 CLI 的 `BOHR_ID`）和 `destination`：

```bash
uv run --package tricycle-agent tricycle-agent run \
  bohr.job.download request.toml --run-id <id> --live
```

已有 run 先 inspect，仅在 `resume.next_action` 为所需下载能力时使用 `resume --run-dir <目录> --live`；批量下载和报告位置见 [bohr-batch-download](../bohr-batch-download/SKILL.md)。单独查询/下载可创建新的操作记录，不对已有 `run.toml` 再执行 `run`。下载是远程读取加本地写入，
必须检查目标目录、保留下载报告，并且不把凭据写入 `run.toml`。

## 失败与恢复

下载失败时保留目标目录和状态文件，修复凭据、网络、job ID（应为 `bohrId`）或目标路径后使用 `resume --live`；
重复下载前确认 adapter 的覆盖和幂等策略，不要删除已有结果。
