---
name: reaction-db-batch-import
description: "Plan one new reaction-database project from exactly one Bohr calculation batch."
---

# 按批次导入反应数据库

一次请求只允许一个数据库内部 project 和一个 Bohr 批次。adapter 必须先创建新 project，
再把该批次 `logs_dir` 下递归发现的 `*.log` 文件交给数据库导入服务；成功结果必须返回
数据库生成的 `project_id`。不能用本地 slug 代替 project ID，也不能把第二个批次追加到同一请求。

导入请求还必须记录 `provenance.workflow`、`provenance.initial_guess_source`；Bohr 来源的批次
必须提供 `bohr_job_group_id` 和 `bohr_project_id`。默认目录是 `downloads/`，选择边界固定为
日志文件，`.gjf`、`.json`、`.xyz` 和 worker `.out` 不会上传。

先执行 dry-run 计划并检查日志数量、总字节数、批次指纹和 project 元数据：

```bash
uv run --package tricycle-agent tricycle-agent plan reaction-db.batch-import request.toml
```

只有显式注入 `ReactionDatabaseAdapter` 后，才允许 live 导入。仓库提供的
`McpReactionDatabaseAdapter` 使用数据库的 Streamable HTTP MCP API；默认 CLI 只在
`--live` 执行阶段从环境变量组合并注入它。默认路径不读数据库凭据、不访问网络、不创建
project。两批数据应使用两份请求，分别获得两个内部 `project_id`。

MCP live 配置至少需要 `TRICYCLE_REACTION_DB_MCP_URL`、`TRICYCLE_REACTION_DB_MCP_TOKEN` 和
`TRICYCLE_REACTION_DB_ORGANIZATION_ID`。仓库提供的 MCP adapter 通过 MCP 的 `create_project`
创建 project；随后按服务限制分组，调用数据库的
`POST /api/upload-batches` 和 `POST /api/upload-batches/{batch_id}/files` multipart 接口传输
日志字节。批量接口只接收文件和队列描述，数据库负责 RustFS staging，并由与单文件接口相同的
durable worker 解析；同机部署也不得改用共享目录或路径 manifest。
`staged`、`queued`、`processing` 等状态表示文件已经进入数据库的持久化队列，receipt 状态为
`submitted`。导入操作只负责完整提交队列，不等待 MolOP 解析完成，因此不会把尚未产生的
`ingestion_status` 或清洗反应数量误报为结果；receipt 会保留每个 batch/item ID、队列状态和失败
样本。TLS 校验默认开启。

实现入口：`packages/tricycle-reaction-db/src/tricycle_reaction_db/models.py`、`plugin.py`、
`mcp_adapter.py`。
