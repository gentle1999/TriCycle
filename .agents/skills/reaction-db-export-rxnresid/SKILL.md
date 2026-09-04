---
name: reaction-db-export-rxnresid
description: "Export mapped reaction energies and phase-resolved compute times for RxnResid."
---

# 导出 RxnResid 数据集和成本统计

使用数据库内部 `project_id`，只导出数据库清洗成功且通过以下实际反应 `reaction_smarts` 筛选的数据；
不要使用 `reaction_class`：

```text
[C,N]=[C,N].[C,N]=[C,N]-[C,N]=[C,N]>>[C,N]1[C,N][C,N][C,N]=[C,N][C,N]1
```

CSV 最低
字段契约为：`mapped_reaction_smiles`、`reaction_free_energy`、`activation_free_energy`，以及
`reactant_compute_seconds`、`transition_state_compute_seconds`、`product_compute_seconds`。
能量单位由 adapter/数据库明确写入结果元数据，耗时单位固定为秒。

这些字段同时支持 RxnResid 训练/微调和按前体、TS、后体统计批次成本。TriCycle 不本地拼接反应、
计算自由能或猜测阶段耗时；adapter 必须读取数据库的 reaction-energy、calculation-frame 和
provenance 数据。

标准 CSV 接口固定为 `POST /api/mapped-reactions/thermodynamics/export.csv`。请求体必须使用
内部 `project_id` 和 `filter_expression`，其中筛选叶子字段必须是 `reaction_smarts`；同时请求
`has_activation_gibbs_free_energy = true` 与 `has_reaction_gibbs_free_energy = true`。禁止使用
`reaction_class`、任意 MCP 查询后本地拼 CSV，或手写另一条导出路由。成功 receipt 必须记录
`RXNRESID_EXPORT_API_CONTRACT` 及标准请求证明，否则插件会拒绝结果。

实现入口：`packages/tricycle-reaction-db/src/tricycle_reaction_db/models.py`、`plugin.py`。
