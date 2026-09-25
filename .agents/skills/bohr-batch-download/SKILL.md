---
name: bohr-batch-download
description: "Download submitted worker artifacts, including partial outputs from failed workers, and preserve the batch download report."
---

# 下载 Bohr 批量结果

## 适用场景与边界

仅在 worker 已完成或需要拉取已有结果时使用；不要用它提交、终止或重新生成任务。

## 输入与输出

输入是包含 `run_dir` 的 TOML 请求，或已有 `run.toml` 的运行目录；输出为
`downloads/` 和 `download-results.tsv`，并更新运行状态。该能力需要 Bohr 凭据，产生远程读取和本地写入。

先确认 `status.tsv` 已同步远程 worker 状态，并检查 `submit-results.tsv` 中保存的是新版 CLI
的公开 `BOHR_ID`；旧批次若保存平台内部 `id`，adapter 会在有 `job_group_id` 时自动映射，并把
`internal_job_id → bohr_id` 保存到运行目录的 `bohr-id-map.tsv`，后续进程优先复用。批量运行推荐使用：

```bash
uv run --package tricycle-agent tricycle-agent resume \
  --run-dir 'runs/<date>/<timestamp>/<id>' --live
```

下载会写入 run 目录的 `downloads/` 和 `download-results.tsv`，可能同时产生本地文件副作用；
每个 `submit-results.tsv` 中有 job ID 的 worker 都会尝试下载，包括 `status.tsv` 标记为
`FAILED` 的 worker，因为失败 worker 可能含有大量成功任务的结果。历史请求中的
`only_succeeded` 字段保留兼容性，但不再用于跳过失败 worker。`resume=True` 只会复用经过完整性
检查的已有结果；对损坏或半下载的目录会写入同盘临时目录，成功后原子替换，旧目录保留为
`.replaced-*` 备份。新版 gateway 下载的 `out.zip` 会校验 CRC，解压标记会绑定压缩包指纹；
旧版本写入的空标记会在校验通过后自动迁移。凭据从环境变量读取；项目 `.env` 会自动发现，
shell 环境变量优先。

## 失败与恢复

网络或单个 worker 失败时保留已有报告，先用 `inspect` 检查，再修复环境变量、`.env` 或 adapter 后执行
`resume --live`。已有目录若被判定为不完整，需显式使用 `resume=True`/`resume --live` 才会
重新拉取；不要删除报告或覆盖原始输入。
