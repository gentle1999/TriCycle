---
name: reaction-db-filter
description: "Apply the database-authoritative reaction rxn-SMARTS predicate to a project."
---

# 数据库反应筛选

请求必须使用数据库内部 `project_id`、`cleaned_only = true`，以及下面这条固定的
`reaction_smarts`：

```text
[#6,#7]=,:[#6,#7].[#6,#7]=,:[#6,#7]-,:[#6,#7]=,:[#6,#7]>>[#6,#7]1[#6,#7][#6,#7][#6,#7]=,:[#6,#7][#6,#7]1
```

TriCycle 不在本地重建反应关系或复制
rxn-SMARTS，也不传递 `reaction_class`；adapter 必须把 `reaction_smarts` 原样传给数据库的
实际反应类型检索，并返回筛选数量和数据库 provenance。

这是只读的数据库操作，但仍需显式注入 adapter。dry-run 只校验 project ID 和筛选约束。

实现入口：`packages/tricycle-reaction-db/src/tricycle_reaction_db/plugin.py`。

筛选计数表示数据库在该项目/SMARTS 下的结果，不表示 ene/diene 输入组合覆盖。
新旧 RXN SMILES 的 mapping 或遍历规则不一致时，按
[反应数据集适配规范](../../../docs/reaction-dataset-adaptation.md) 核查结构等价性；
不能用字符串差异或本地匹配组合数减少推断数据库缺数据。
后续正式导出和适配使用 [reaction-db-export-rxnresid](../reaction-db-export-rxnresid/SKILL.md)。
