---
name: reaction-db-export-rxnresid
description: "Export mapped reaction energies and phase-resolved compute times for RxnResid."
---

# 导出 RxnResid mapped-reaction 数据集

使用数据库内部 `project_id`。本次 ground-truth 项目 ID 固定为
`00000000-0000-7000-8000-000000000201`。请求必须设置 `cleaned_only = true`，并按数据库的实际
`reaction_smarts` 筛选；不要使用 `reaction_class`：

```text
[#6,#7]=,:[#6,#7].[#6,#7]=,:[#6,#7]-,:[#6,#7]=,:[#6,#7]>>[#6,#7]1[#6,#7][#6,#7][#6,#7]=,:[#6,#7][#6,#7]1
```

## 数据库 MCP 查询

通过注入的 `McpReactionDatabaseAdapter` 执行 `reaction-db.export-rxnresid`。adapter 使用数据库 MCP
应用的 `compose_query`，不调用 REST CSV 路由：

1. 调用 `MappedReactionQueryService.list_mapped_reactions`，传入 `project_id` 和完整
   `reaction_smarts`。分页读取所有 `items`，根据返回的 `page` 信息继续请求，直到遍历完项目结果。
2. 对每个 mapped-reaction summary，以其 `id` 和同一个 `project_id` 调用
   `ReactionEnergyQueryService.get_mapped_reaction_thermodynamics`。
3. 使用数据库返回的 mapped reaction SMILES 和 materialized thermodynamics profile。能量和阶段耗时
   必须取自同一个 profile；不得从 calculation log 本地重算、拼接或猜测。

查询必须通过注入的 MCP adapter 使用项目限定的查询构造；不要在 skill 流程中临时拼接任意
GraphQL。每次调用都必须使用同一个 `project_id`。映射查询使用 `reaction_smarts` 参数，不使用
`reaction_class`，也不需要额外的 `filter_expression` 或 REST `has_*` 参数。当前 `compose_query`
复杂度上限为 250；若将多个 thermodynamics 查询合并为 GraphQL aliases，按本字段投影每批最多 24 条，
超出时拆分。数据库返回的
项目内 mapped-reaction 记录及 thermodynamics materialization 是清洗后数据的权威来源；没有
materialized profile 时不能回退到本地日志。

## 数据集字段和单位

CSV 最低字段契约为以下六列：

- `mapped_reaction_smiles`
- `reaction_free_energy`
- `activation_free_energy`
- `reactant_compute_seconds`
- `transition_state_compute_seconds`
- `product_compute_seconds`

自由能从 profile 的 `reaction.gibbs_free_energy_kcal_mol` 和
`activation.gibbs_free_energy_kcal_mol` 读取，单位为 `kcal/mol`。阶段耗时从同一 profile 的
`reactants_running_time_seconds`、`transition_state_running_time_seconds` 和
`products_running_time_seconds` 读取，单位为秒。若阶段耗时为 null，CSV 对应单元格留空；不得估算。
能量或 activation/reaction profile 缺失时，该 mapped reaction 不满足数据集契约；从 CSV 排除，并在
receipt 记录排除数量。不得填造数值。

当前 MCP adapter 使用数据库返回的第一个 materialized profile（`profiles[0]`）；一行中的能量和阶段耗时
必须全部取自该 profile，不得把不同 theory、温度或 profile 的字段混在一起。没有 profile、profile
格式无效时导出失败，不得改查本地文件。profile、reaction/activation 对象或任一必需自由能缺失时，
排除该条并记录原因和数量。必需 CSV 列仍须输出；源阶段耗时为 null 时留空，不得估算。

## 输出和 provenance

CSV 列映射如下：

| MCP 返回字段 | CSV 列 |
| --- | --- |
| `mapped_reaction_smiles` | `mapped_reaction_smiles` |
| `reaction.gibbs_free_energy_kcal_mol` | `reaction_free_energy` |
| `activation.gibbs_free_energy_kcal_mol` | `activation_free_energy` |
| `reactants_running_time_seconds` | `reactant_compute_seconds` |
| `transition_state_running_time_seconds` | `transition_state_compute_seconds` |
| `products_running_time_seconds` | `product_compute_seconds` |

成功 receipt 必须声明 `RXNRESID_EXPORT_API_CONTRACT`，并保留 `compose_query` provenance，至少包含
MCP app name、查询的 service/method、`project_id`、原样 `reaction_smarts`、mapped-reaction 分页数、
导出行数以及 `energy_unit = "kcal/mol"`、`timing_unit = "seconds"`、`cleaned_only = true`。
同时记录因缺少完整热化学值而排除的记录数。不得记录 bearer token。任一 MCP 查询失败、返回错误、
项目 ID 不一致或 receipt 缺少上述证明时，不得把导出标记为成功。

运行时从环境读取 `TRICYCLE_REACTION_DB_MCP_URL`、`TRICYCLE_REACTION_DB_MCP_TOKEN` 和可选的
`TRICYCLE_REACTION_DB_MCP_APP_NAME`；不得把凭据写进 TOML。请求示例见
`configs/example-reaction-db-rxnresid-export.toml`。live 执行入口：

```bash
uv run --package tricycle-agent tricycle-agent run \
  reaction-db.export-rxnresid \
  configs/example-reaction-db-rxnresid-export.toml \
  --run-id rxnresid-ground-truth-export --live
```

CSV、receipt 和 run 记录放在 `runs/YYYY-MM-DD/<本地时间戳>/record/`；若设置了
`TRICYCLE_RUN_ROOT`，将 `runs/` 替换为该根目录。
