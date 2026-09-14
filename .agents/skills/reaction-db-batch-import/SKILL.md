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
`TRICYCLE_REACTION_DB_ORGANIZATION_ID`。仓库提供的 MCP adapter 先创建 project，再对每个选中
的 `.log` 调用 `upload_calculation_log`，用标准 Base64 传输文件字节，并携带相对路径、文件
SHA-256 和大小；同机部署也不得改用共享目录或路径 manifest。数据库返回的
`ingestion_status` 是文件是否成功解析的依据，receipt 会保留上传/解析统计和失败样本。TLS
校验默认开启。

实现入口：`packages/tricycle-reaction-db/src/tricycle_reaction_db/models.py`、`plugin.py`、
`mcp_adapter.py`。
