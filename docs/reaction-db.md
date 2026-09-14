# 反应数据库工作流约束

`tricycle-reaction-db` 只负责声明输入边界、生成审计计划和定义 adapter 契约。数据库负责
日志解析、分子/拓扑/几何关系、TS 证据、反应关系和最终清洗；TriCycle 不在本地复制这些业务规则。

## 项目与批次

一个 `reaction-db.batch-import` 请求只做一件事：创建一个数据库内部 project，并提交一个
Bohr 计算批次的日志。adapter 成功后必须返回数据库内部 `project_id`。因此两批 Bohr 结果应
执行两份请求并得到两个 project ID；任何过滤或导出都必须使用对应的 project ID。

批次目录只递归选择 `downloads/` 下的 `*.log`。输入 `.gjf`、元数据 `.json`、结构 `.xyz`、
worker `.out` 和其他批次目录不在上传范围内。请求必须携带以下 lineage：

- `provenance.workflow`；
- `provenance.initial_guess_source`；
- Bohr 来源批次的 `bohr_job_group_id` 和 `bohr_project_id`；
- 自动生成或显式提供幂等键。

## 核心操作

| 能力 | 输入 | 数据库侧行为 | 最低输出契约 |
| --- | --- | --- | --- |
| `reaction-db.batch-import` | 一个 project 创建描述 + 一个批次 | 创建 project，上传日志并完成解析/关系整理 | `project_id`、导入统计、清洗统计 |
| `reaction-db.filter` | `project_id` + 固定 `reaction_smarts` | 使用数据库的实际反应 SMARTS 检索且只取清洗成功数据；不使用 `reaction_class` | 筛选数量、实际 SMARTS 和查询 provenance |
| `reaction-db.export-rits` | `project_id` + CSV 路径 | 对同一筛选结果导出 mapped reaction 和 TS 结构 | `mapped_reaction_smiles`、`ts_structure` |
| `reaction-db.export-rxnresid` | `project_id` + CSV 路径 | 导出反应能、活化能和分阶段计算耗时 | mapped reaction、两类自由能、前体/TS/后体秒数 |

筛选使用的实际数据库 rxn-SMARTS 固定为：

```text
[C,N]=[C,N].[C,N]=[C,N]-[C,N]=[C,N]>>[C,N]1[C,N][C,N][C,N]=[C,N][C,N]1
```

请求中的 `reaction_filter = "cycloaddition"` 仅保留为本地操作兼容标签；adapter 必须将
`reaction_smarts` 原样传给数据库的 `reaction_smarts` 查询参数，不能转换成显式的
`reaction_class` 条件，也不能在本地重做反应分类。RitS 的 TS 结构默认以 `xyz` 表示。RxnResid 的能量单位由数据库/adapter 明确记录；耗时单位
固定为秒，阶段固定为 `reactant`、`transition_state`、`product`。CSV 的实际序列化、数据库
查询和文件下载由注入的 `ReactionDatabaseAdapter` 完成。仓库内置的
`McpReactionDatabaseAdapter` 使用数据库的 Streamable HTTP MCP endpoint 创建 project，并通过
数据库批量文件接口提交日志：先调用 MCP `create_project`，再按服务限制调用
`POST /api/upload-batches` 创建队列和 `POST /api/upload-batches/{batch_id}/files` multipart
上传文件；筛选和导出通过数据库 MCP 的 `compose_query` 调用 UseCase schema。批量接口与单文件
接口共用数据库的 RustFS staging 和 durable worker，TriCycle 只传输文件字节和队列描述，不依赖
数据库服务可见的共享目录，也不因同机部署而切换实现。默认批量模式可通过
`TRICYCLE_REACTION_DB_MCP_UPLOAD_MODE=batch` 明确配置；`mcp` 模式保留为旧服务的逐文件
`upload_calculation_log` 兼容路径。每个批量响应返回 durable batch/item；adapter 在所有文件
入队后返回 `submitted`，保留队列 ID 和初始状态，不等待异步 MolOP 解析，也不会在提交阶段填充
清洗反应数量。失败文件/批次会继续尝试其余输入并记录在 receipt 中。

live CLI 只在执行阶段注入该 adapter，dry-run 不读取数据库凭据。运行前通过环境变量提供
`TRICYCLE_REACTION_DB_MCP_URL`、`TRICYCLE_REACTION_DB_MCP_TOKEN`、
`TRICYCLE_REACTION_DB_ORGANIZATION_ID`。如果服务使用内部 CA，应配置
`TRICYCLE_REACTION_DB_MCP_CA_BUNDLE`；不要为了绕过证书问题默认关闭 TLS 校验。

## 标准数据库导出门禁

导出 adapter 不能把任意本地日志解析结果拼成一个“成功”的 CSV。成功返回的
`ReactionSpaceExportReceipt` 必须带 `api_contract` 和标准 HTTP API 或 MCP 请求证明；插件会在
生成 `TaskResult` 前校验它们。MCP adapter 的证明记录 `compose_query` 所访问的 database
service/method、project ID 和固定 reaction SMARTS。

- 旧 HTTP adapter 的 RxnResid 路径固定使用
  `POST /api/mapped-reactions/thermodynamics/export.csv`；请求体必须包含当前内部
  `project_id`、`filter_expression`（其 JSON 叶子字段为 `reaction_smarts`）、
  `has_activation_gibbs_free_energy = true` 和 `has_reaction_gibbs_free_energy = true`。
- MCP adapter 的 RxnResid 路径使用 `compose_query` 调用
  `MappedReactionQueryService.list_mapped_reactions` 和
  `ReactionEnergyQueryService.get_mapped_reaction_thermodynamics`，再将数据库返回的字段
  写入 CSV。
- 旧 HTTP adapter 的 RitS 路径使用标准 `GET /api/mapped-reactions`、mapped-reaction detail
  和 `GET /api/depictions/geometry/{geometry_id}.xyz`；MCP adapter 对应使用
  `MappedReactionQueryService.list_mapped_reactions`、`get_mapped_reaction` 和
  `GeometryQueryService.get_geometry`。两种 adapter 都不能使用 `reaction_class` 或本地日志
  推断。

这些 HTTP 路由和请求构造器集中在
`packages/tricycle-reaction-db/src/tricycle_reaction_db/contracts.py`；adapter 应直接复用
`build_rxnresid_csv_export_request` 和 `build_rits_mapped_reaction_requests`，不要手写路由或
过滤体。

## 基本查询能力

基本查询是只读的，且必须按 project 过滤并保留数据库返回的分页游标和 provenance：

| 查询层 | 用途 |
| --- | --- |
| project / artifact / ingestion | 确认项目范围、原始日志、上传和解析状态 |
| formula / topology / geometry | 查看分子身份、拓扑去重和可复用结构 |
| calculation protocol / segment / frame / result | 查看方法、路径阶段、帧、能量和计算结果 |
| transition-state inference | 查看虚频、端点变化和 TS 推断证据 |
| logical / mapped reaction | 查看反应关系、映射和路径节点 |
| reaction energy | 查看反应自由能、活化自由能和热力学摘要 |
| workflow manifest / artifact binding | 追踪批次、文件和工作流版本的绑定 |

TriCycle 只描述这些查询的用途，不把远端 GraphQL/MCP schema 硬编码进 core。具体 MCP 查询
通过 adapter 实现；如果数据库提供批量导出，应优先使用数据库导出而不是本地拼接 CSV。

## 当前两批结果的请求方式

第一批和第二批各自使用一份 `reaction-db.batch-import` TOML。两份请求可以共享 workflow 和
初猜来源说明，但必须分别声明自己的批次目录、Bohr job group/project 和 project slug。拿到
两个返回的 `project_id` 后，再分别执行 filter 和两个 export 操作；不要把两个 project 的数据
在本地合并后再筛选。
