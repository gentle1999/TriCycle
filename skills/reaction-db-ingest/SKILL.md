---
name: reaction-db-ingest
description: "Validate reaction records and project-scoped calculation-log database workflows."
---

# 反应数据库录入

## 适用场景

当一次计算、训练或推理产生了需要持久化的反应记录、结构文件或 provenance 时使用；批量计算
日志使用对应的 project-scoped batch workflow。

## 输入

- `ReactionRecord`：`reaction_id`、mapped `reaction_smiles`、反应物/产物和条件；
- 批次导入：一次请求只允许创建一个数据库内部 project，并绑定一个计算批次的 `*.log`；
- 数据库项目创建成功后，后续操作只使用返回的 `project_id`，不使用本地 slug；
- 关联的结构、日志、能量或模型产物 artifact；
- `idempotency_key`、来源 `source` 和可选 metadata；
- 一个实现了 `ReactionRepository.upsert` 的 repository。

## 工作流

1. 先调用 `ReactionIngestRequest` 或批次请求做非空、项目边界和文件选择校验。
2. 用 `ReactionDatabasePlugin.plan("ingest", ...)` 生成计划。
3. 在写入前检查 project/batch provenance、日志数量、计算状态和数据版本。
4. 需要只校验时设置 `validate_only=True`；需要写入时显式关闭 dry-run 并注入 repository。
5. 记录 upsert 结果和幂等键，避免重复录入同一计算产物。

## 输出与约束

- 规划输出是 `TaskPlan`；
- 成功写入输出 `TaskResult(status=succeeded, external_id=reaction_id)`；
- 默认不连接数据库；
- 不覆盖已有记录的业务字段，冲突合并策略要由数据库插件规格明确。

批次导入后，使用数据库自己的实际反应 `reaction_smarts` 规则筛选清洗成功的反应，而不是
显式 `reaction_class` 分类。固定模式为
`[#6,#7]=,:[#6,#7].[#6,#7]=,:[#6,#7]-,:[#6,#7]=,:[#6,#7]>>[#6,#7]1[#6,#7][#6,#7][#6,#7]=,:[#6,#7][#6,#7]1`。RitS 导出至少包含
`mapped_reaction_smiles` 和 `ts_structure`；RxnResid 导出至少包含 mapped reaction SMILES、反应
自由能、活化自由能以及前体/TS/后体计算秒数。TriCycle 不在本地猜测这些关系、能量或耗时。

数据库基本查询按 artifact/ingestion、formula/topology/geometry、calculation frame/result、TS
inference、logical/mapped reaction、energy、workflow manifest 分层；adapter 负责分页、项目过滤和
provenance 关联。

实现入口：`packages/tricycle-reaction-db/src/tricycle_reaction_db/plugin.py`。
