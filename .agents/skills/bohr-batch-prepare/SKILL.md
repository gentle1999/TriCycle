---
name: bohr-batch-prepare
description: "Prepare an auditable local Bohr batch run from a TOML request without submitting remote jobs."
---

# 准备 Bohr 批量运行目录

## 适用场景

当需要展开任务文件、分配 worker、生成 `batch.toml` 和 `submit-plan.tsv` 时使用。

## 输入与输出

- 输入：TOML，包含任务 glob、worker 数量、脚本、项目/机器/镜像等字段；
- 输出：空目录中的 `run.toml`、`request.toml`、`batch.toml`、worker 文件和提交计划；
- 只产生本地文件，不读取 Bohr 凭据，不访问网络。

## 执行与检查

```bash
uv run --package tricycle-agent tricycle-agent run \
  bohr.batch.prepare request.toml --run-id <id> --run-dir runs/<id>
uv run --package tricycle-agent tricycle-agent inspect --run-dir runs/<id>
```

输出目录必须为空；检查任务数量、worker 分片和 `submit-plan.tsv` 后，才进入提交能力。
不要在本 Skill 中提交或终止远程任务。

## 不适用与失败恢复

已有运行目录需要继续提交时使用 `bohr.batch.submit` 或 `resume`，不要再次 prepare。
请求校验失败或目录非空时修正 TOML、选择新的 `run-id` 和空目录后重试；该本地阶段不自动恢复旧目录。
