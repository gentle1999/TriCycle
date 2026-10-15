---
name: bohr-batch-prepare
description: "Prepare an auditable local Bohr batch run from a TOML request without submitting remote jobs."
---

# 准备 Bohr 批量运行目录

## 适用场景

当需要展开任务文件、分配 worker、生成 `batch.toml` 和 `submit-plan.tsv` 时使用。

## 输入与输出

- 输入：TOML，任务来源选择 `task_patterns` glob 或 `task_defaults` + `task_specs`；另包含 worker 数量、脚本、项目/机器/镜像等字段；
- 输出：空目录中的 `run.toml`、`request.toml`、`batch.toml`、worker 文件和提交计划；
- 只产生本地文件，不读取 Bohr 凭据，不访问网络。

## 执行与检查

```bash
uv run --package tricycle-agent tricycle-agent run \
  bohr.batch.prepare request.toml --run-id <id>
uv run --package tricycle-agent tricycle-agent inspect --run-dir 'runs/<date>/<timestamp>/<id>'
```

输出目录必须为空；检查任务数量、worker 分片和 `submit-plan.tsv` 后，才进入提交能力。
不要在本 Skill 中提交或终止远程任务。

## 不适用与失败恢复

已有运行目录需要继续提交时使用 `bohr.batch.submit` 或 `resume`，不要再次 prepare。
请求校验失败或目录非空时修正 TOML、选择新的 `run-id` 和空目录后重试；该本地阶段不自动恢复旧目录。

## 已绑定 SAR 协议的 batch

包含当前 method 引用的请求在 run 根目录只生成一份 `training-protocol.json`，绑定公共输入和逐 worker 任务哈希；`batch.toml` 保存其 SHA256。worker 输入先在临时目录生成，再按内容保存到项目资产库；`batch.toml.input_assets` 与每个 worker 的 `input-assets.json` 绑定完整输入，run 不保留输入实体或软链接。硬链接仅作为资产库内部可选优化。修改任何已绑定文件会在提交前被拒绝。提交只接受 run 根目录的当前 marker 和绑定，不读取逐 worker 历史格式；新任务使用当前包内后端配置。

任务参数相同时优先使用内嵌任务表：公共参数写一次，每行只记录唯一 `name` 与 fold/seed 等差异。不能在任务行覆盖公共字段。准备时自动展开为 worker 所需的 TOML，按表顺序均衡分片；不用额外创建逐任务源 TOML 或改文件名配平 worker。
