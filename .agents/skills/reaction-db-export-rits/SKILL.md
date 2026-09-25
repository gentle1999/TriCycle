---
name: reaction-db-export-rits
description: "Export mapped reaction SMILES and transition-state structures for RitS."
---

# 导出 RitS 数据集

使用数据库内部 `project_id`，通过数据库 MCP 对该项目应用以下实际反应 `reaction_smarts` 筛选，
再导出 CSV。不要使用 `reaction_class`，也不要读取本地计算日志推断映射反应或 TS 结构：

```text
[#6,#7]=,:[#6,#7].[#6,#7]=,:[#6,#7]-,:[#6,#7]=,:[#6,#7]>>[#6,#7]1[#6,#7][#6,#7][#6,#7]=,:[#6,#7][#6,#7]1
```
每行的最低数据契约是：`mapped_reaction_smiles` 和对应的 `ts_structure`；结构格式默认 `xyz`。
RitS 导出请求要求显式提供数据库内部 `project_id`。若 ID 未知，先通过数据库 MCP 的
`list_projects` 查询，并使用目标 ground-truth 项目的 ID；不要把 RxnResid 导出使用的固定 ID
当成 RitS 的默认值。

使用数据库 MCP `export_mapped_reaction_transition_state_geometries` 分页工具读取 mapped reaction 和 verified TS geometry。
工具按 `after_binding_id` 游标返回 bounded page，字段包括 `records`、`has_more` 和
`next_after_binding_id`；每条记录的 `key` 是 mapped reaction SMILES，`value.geometry.atoms` 含 Å
坐标和每个原子的 `atom_map_number`。不要把 Streamable HTTP 的传输封装误当成这个应用级分页导出
能力，也不要改用同名 REST 路由。
geometry 记录 schema 支持 `mapped-reaction-ts-geometry-v1` 和 `v2`；当前部署可返回 v2，新增的
geometry 元数据不替代也不改变 `geometry_id`、`coordinate_units`、`atoms` 等 XYZ 导出所需字段。
兼容时应继续验证相同的核心字段和 atom-map 契约，而非因 schema 版本变化丢弃记录。

该 geometry 工具只按 project/cursor 分页，不接收 `reaction_smarts`。因此还必须通过 MCP
`compose_query` 调用 `MappedReactionQueryService.list_mapped_reactions`，带上完整固定 `project_id`
和 `reaction_smarts`，按服务分页结果构造数据库筛选通过的 mapped-SMILES 集合；随后仅保留 geometry
页中 `key` 命中该集合的记录。不要在本地重新解释 SMARTS，也不要用 `reaction_class` 或日志替代
数据库筛选。

逐页处理 geometry 记录，不累积全部 XYZ：验证 `schema`、project scope、坐标单位为 angstrom、每条
记录的原子映射唯一且完整为 `1..N`，按 `atom_map_number` 递增排序其元素/坐标，再物化为 XYZ。记录
含重复 mapped-SMILES 时不要去重（一个反应可绑定多个 TS geometry）。全部页结束后，如果筛选集合中
有任何反应没有对应 verified geometry，必须失败，不得静默丢弃或猜测顺序。

将结果逐页写入本地临时 CSV，列名为 `mapped_reaction_smiles` 和 `ts_structure`，完整成功后原子
替换目标文件。成功 receipt 必须记录 `reaction-db.mcp.mapped-reaction-geometry.v1`、筛选
`compose_query` 与 geometry 导出工具的 provenance、游标页数、扫描记录数、筛选后导出行数。若部署
MCP 未注册该 geometry 导出工具，应明确失败；不要回退为 REST 路由或串行逐反应查询。数据库提供的 `create_units_ts_dataset` 是 UniTS NPY 导出任务，不是
RitS 数据契约；不要将其作为 RitS mapped-SMILES/TS-XYZ 导出接口。

导出文件是本地 CSV；成功结果记录 project ID、筛选器、结构格式、行数和数据库 MCP provenance。
dry-run 不查询数据库、不写文件。Live 执行需由应用注入 `McpReactionDatabaseAdapter`，并通过
环境变量提供 MCP endpoint 和 token。先检查计划，再显式 live 导出：

此操作生成的是数据库权威源数据 CSV，不是可直接传给 `rits.train` 的预处理数据目录。
训练请求的 dataset artifact 必须符合请求所用 RitS `source_revision` 的目录和预处理格式；
先使用该 revision 支持的预处理流程生成并检查训练数据文件，再规划训练任务。若该 revision
的预处理器要求 `AAM` 列、拼接 XYZ corpus 或相邻的正/逆反应样本，必须在转换时按其契约生成这些
输入，并保留每一对对应同一个 TS 几何。还必须检查反应物/产物在每个 atom-map 上的元素一致，
并检查已按递增 atom-map 排序的 XYZ 原子元素与映射反应一致；不兼容样本要逐条写入拒绝清单和
manifest 计数，不能静默丢弃或猜测映射；不要把本 CSV 直接改名当成预处理产物。

```bash
uv run --package tricycle-agent tricycle-agent plan \
  reaction-db.export-rits request.toml --run-id rits-export

uv run --package tricycle-agent tricycle-agent run \
  reaction-db.export-rits request.toml --run-id rits-export --live
```

实现入口：`packages/tricycle-reaction-db/src/tricycle_reaction_db/models.py`、`plugin.py`、
`repository.py`、`mcp_adapter.py` 和 `contracts.py`。
