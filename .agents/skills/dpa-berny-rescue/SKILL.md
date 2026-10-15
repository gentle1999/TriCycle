---
name: dpa-berny-rescue
description: "Prepare a DPA-Berny or Gaussian rescue run from an existing run directory using a TOML request."
---

# 准备 DPA-Berny 救援运行

## 适用场景

当已有批量运行的结果和失败明细，需要跳过成功任务、按规则重试失败任务时使用。

## 输入与输出

- 输入：TOML，包含 `source_run`、worker 数量，以及可选下载目录和重试策略；
- 输出：新的本地 run 目录、`rescue-summary.tsv`、`batch.toml` 和可继续提交的任务；
- 不会自动重新提交远程任务。

## 执行

```bash
uv run --package tricycle-agent tricycle-agent run \
  dpa-berny.rescue request.toml --run-id <id> --run-dir 'runs/<date>/<timestamp>/<id>'
```

先检查救援计划和被跳过的成功任务，再使用 `bohr.batch.submit`。源目录不会被覆盖。
救援同样要求 DPA 使用 GPU、纯 Gaussian 使用 CPU；源配置不符合时拒绝准备。
新 run 从包内当前实现生成 Bash worker，从源 `batch.toml.input_assets` 解析任务输入和模型资产；
临时准备后将新输入按内容入库，run 只保存引用，不复制旧启动脚本。既有物理输入目录仍可读取。
从修正后的请求创建新 run，保留源记录；不能把 DPA 的 CPU 结果作为 GPU smoke 通过证据。

## 失败与恢复

救援计划生成失败时检查源目录和失败明细，修正 TOML 后创建新的 rescue run；不要覆盖源目录，
也不要在本 Skill 中直接重提远程 worker。
