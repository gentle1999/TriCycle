# TriCycle Agent Skills（兼容索引）

新的 Codex 仓库级发现入口是 [`../.agents/skills/`](../.agents/skills/)。本目录保留旧版按组件组织的说明，
用于迁移期间兼容已有链接；新的能力 ID、调用命令和安全边界以 `.agents/skills` 为准。

这些 skill 与 `packages/` 中的实现一一对应。它们描述调用边界和检查清单；真实平台命令、数据库字段和模型参数通过 adapter 注入。

| Skill | 对应包 | 主要任务 |
| --- | --- | --- |
| `bohr-compute` | `tricycle-bohr` | 规划、提交、查询和下载 Bohr 计算 |
| `bohr-submit` | `tricycle-bohrsubmit` | 批量 staging、Gaussian/DPA-Berny、生命周期和救援 |
| `reaction-db-ingest` | `tricycle-reaction-db` | 校验并幂等录入反应记录 |
| `reaction-db-batch-import` | `tricycle-reaction-db` | 每批创建内部 project 并导入计算日志 |
| `reaction-db-filter` | `tricycle-reaction-db` | 用数据库 rxn-SMARTS 筛选清洗成功的环加成反应 |
| `reaction-db-export-rits` | `tricycle-reaction-db` | 导出 mapped reaction 与 TS 结构 |
| `reaction-db-export-rxnresid` | `tricycle-reaction-db` | 导出能量、阶段耗时和成本统计数据 |
| `sar-model` | `tricycle-sar` | RxnResid（SAR 兼容命名）训练/微调/推理并提交 Bohr |
| `rits-model` | `tricycle-rits` | RitS 训练/微调/推理并提交 Bohr |
| `sampling-strategy` | `tricycle-sampling` | 从候选空间选择下一批反应 |
| `reactionopt-config` | `tricycle-reactionopt` | 生成 ReactionOpt 配置 |
| `reactionopt-profile` | `tricycle-reactionopt` | 从 mapped reaction 生成独立端点与 RitS TS profile |

Skill 的调用顺序通常是：`reactionopt-config → sampling-strategy → bohr-submit/bohr-compute → reaction-db-batch-import → reaction-db-filter → reaction-db-export-* → sar-model/rits-model`。
