---
name: reaction-db-export-rits
description: "Export mapped reaction SMILES and transition-state structures for RitS."
---

# 导出 RitS 数据集

使用数据库内部 `project_id`，先由数据库应用以下实际反应 `reaction_smarts` 筛选，再导出 CSV；
不要使用 `reaction_class`：

```text
[C,N]=[C,N].[C,N]=[C,N]-[C,N]=[C,N]>>[C,N]1[C,N][C,N][C,N]=[C,N][C,N]1
```
每行的最低数据契约是：`mapped_reaction_smiles` 和对应的 `ts_structure`；结构格式默认 `xyz`。
TriCycle 不从本地日志推断 mapped reaction 或 TS 结构，adapter 必须从数据库对象/几何数据导出。

当前数据库没有一个同时返回 mapped reaction 和 TS 的单一 CSV 路由。adapter 必须使用标准
`GET /api/mapped-reactions`（带 `project_id`、固定 `reaction_smarts`）、mapped-reaction detail
和 `GET /api/depictions/geometry/{geometry_id}.xyz`，再将返回对象物化为本地 CSV。成功 receipt
必须记录 `RITS_EXPORT_API_CONTRACT` 和这三类标准请求；不能使用 `compose_query`、
`reaction_class` 或直接读取本地计算日志来替代。

导出文件是本地 CSV，成功结果会记录 project ID、筛选器、结构格式和行数。dry-run 不查询数据库、
不写文件。

实现入口：`packages/tricycle-reaction-db/src/tricycle_reaction_db/models.py`、`plugin.py`。
