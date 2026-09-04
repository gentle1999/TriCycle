---
name: reaction-db-filter
description: "Apply the database-authoritative reaction rxn-SMARTS predicate to a project."
---

# 数据库反应筛选

请求必须使用数据库内部 `project_id`、`cleaned_only = true`，以及下面这条固定的
`reaction_smarts`：

```text
[C,N]=[C,N].[C,N]=[C,N]-[C,N]=[C,N]>>[C,N]1[C,N][C,N][C,N]=[C,N][C,N]1
```

`reaction_filter = "cycloaddition"` 仅是本地兼容标签。TriCycle 不在本地重建反应关系或复制
rxn-SMARTS，也不传递 `reaction_class`；adapter 必须把 `reaction_smarts` 原样传给数据库的
实际反应类型检索，并返回筛选数量和数据库 provenance。

这是只读的数据库操作，但仍需显式注入 adapter。dry-run 只校验 project ID 和筛选约束。

实现入口：`packages/tricycle-reaction-db/src/tricycle_reaction_db/plugin.py`。
