---
name: reaction-db-ingest
description: "Validate reaction records or plan project-scoped calculation-log database workflows."
---

# 反应数据库工作流

## 适用场景

当反应记录需要兼容录入，或 Bohr 计算日志要按批次进入反应数据库时使用。大批量计算日志应
使用 `reaction-db-batch-import`；筛选和训练数据导出分别使用对应的 reaction-db skills。

## 安全边界

默认 dry-run 只校验并生成计划，不写远程数据库。真实批次导入必须由应用注入
`ReactionDatabaseAdapter`，由 adapter 创建数据库内部 project 并返回 `project_id`；凭据不能复制
进请求或运行状态。

## 输入与执行

兼容单反应请求使用 `reaction_id`、`reaction_smiles`、可选 `conditions`、`provenance` 和
artifact 字段。批次请求一次只能声明一个 project 创建描述和一个批次目录，并且只递归选择
`downloads/` 下的 `*.log`；必须保留 workflow、初猜来源、Bohr job group/project provenance。

导出操作一律使用数据库内部 `project_id`，且由数据库用固定的实际反应
`reaction_smarts` 筛选清洗成功数据（不是 `reaction_class` 分类）：

```text
[#6,#7]=,:[#6,#7].[#6,#7]=,:[#6,#7]-,:[#6,#7]=,:[#6,#7]>>[#6,#7]1[#6,#7][#6,#7][#6,#7]=,:[#6,#7][#6,#7]1
```

- RitS：`mapped_reaction_smiles + ts_structure`；
- RxnResid：`mapped_reaction_smiles + reaction/activation free energy + reactant/TS/product seconds`。

应用层保留 `run.toml` 作为审计入口，不要猜测未定义的数据库 schema，也不要在本地重新推断反应关系。

## 失败与恢复

校验失败时修正 TOML 并使用新的运行实例。远程写入失败时先检查 adapter 是否已完成幂等 project
创建/导入，再由注入 adapter 的应用恢复；不要在没有 adapter 状态的情况下盲目重复写入。

## 基本查询能力说明

数据库查询按对象层次使用：artifact/ingestion 查看原始日志及解析状态；formula/topology/geometry
查看化学身份和结构；calculation protocol/segment/frame/result 查看计算过程和结果；transition
state inference 查看 TS 证据；logical/mapped reaction 查看反应关系；reaction energy 查看自由能
和活化能；workflow manifest 查看批次与 artifact 绑定。分页、项目过滤和 provenance 应由数据库
adapter 负责，TriCycle 不复制这些查询 schema。
