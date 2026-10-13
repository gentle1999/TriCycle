---
name: bohr-batch-download
description: "Download submitted worker artifacts, including partial outputs from failed workers, and preserve the batch download report."
---

# 下载 Bohr 批量结果

## 适用场景与边界

仅在 worker 已完成或需要拉取已有结果时使用；不要用它提交、终止或重新生成任务。

## 输入与输出

输入是包含 `run_dir` 的 TOML 请求，或已有 `run.toml` 的运行目录；输出为
`downloads/` 和 `downloads/download-results.tsv`，并更新运行状态。该能力需要 Bohr 凭据，产生远程读取和本地写入。

先确认 `status.tsv` 已同步远程 worker 状态，并检查 `submit-results.tsv` 中保存的是新版 CLI
的公开 `BOHR_ID`；不接受依赖内部 ID 映射的旧运行目录。先 inspect，确认 `resume.next_action = "bohr.batch.download"` 后使用：

```bash
uv run --package tricycle-agent tricycle-agent resume \
  --run-dir 'runs/<date>/<timestamp>/<id>' --live
```

`resume` 不是固定下载命令。下一动作不匹配时，通过注入 gateway 的 `BohrSubmitPlugin.download` 单独下载，不借此推进提交或其他阶段。

下载报告位于 `downloads/download-results.tsv`；exploration campaign 会另将其复制到批次根目录供采集证据核验。普通下载不会自动生成该根目录副本。
每个 `submit-results.tsv` 中有 job ID 的 worker 都会尝试下载，包括 `status.tsv` 标记为
`FAILED` 的 worker，因为失败 worker 可能含有大量成功任务的结果。请求不接受 `only_succeeded` 字段。`resume=True` 只会复用经过完整性
检查的已有结果；对损坏或半下载的目录会写入同盘临时目录，成功后原子替换，旧目录保留为
`.replaced-*` 备份。新版 gateway 下载的 `out.zip` 会校验 CRC，解压标记会绑定压缩包指纹；
空标记不能证明解压完整，必须重新校验压缩包并解压；损坏的压缩包按恢复选项重新下载。凭据从环境变量读取；项目 `.env` 会自动发现，
shell 环境变量优先。

## 失败与恢复

网络或单个 worker 失败时保留已有报告，先用 `inspect` 检查，再修复环境变量、`.env` 或 adapter 后执行
相同的下载动作。修复不完整目录需向包内 `download(..., resume=True)` 显式传入恢复选项；CLI 的 `resume` 只复用原请求的 `resume` 字段，不会自动将它设为 true。不要删除报告或覆盖原始输入。
