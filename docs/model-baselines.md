# SAR 模型基准

多模型基准复用 [SAR 包](../packages/tricycle-sar/README.md)的当前后端。模型清单、环境与执行器由 [catalog](../packages/tricycle-sar/src/tricycle_sar/backends/catalog.json)提供；数据与实验绑定见[基准配置](../configs/example-sar-benchmark.toml)，操作流程见 [sar-benchmark 技能](../.agents/skills/sar-benchmark/SKILL.md)。

## 共同实验条件

同一底物组的全部路径位于同一外折，验证集只从该外折训练部分产生。所有模型共用数据与划分；完整 OOF 必须覆盖每条路径且恰好一次。以绑定的真实标签重算指标，分别报告折间 MAE 均值/样本标准差与 pooled OOF 指标，不按测试误差挑选重跑结果。

## 准备、执行与核验

先本地 `benchmark prepare/check`，再通过 `requests --group-config` 生成任务。默认只准备正式训练请求，smoke 需显式启用；模型前后向 probe 与训练后预测分别验收。Bohr/内网执行器按 catalog 选择，已有 job ID 进入状态查询与下载，失败重跑创建新 run。

完成全部计划任务后，`benchmark publish` 导出数据、统一划分、合并 OOF、指标、方法绑定和 manifest；`results verify` 独立重算科学结果。完整命令及输出契约见 [SAR 实验记录与归档](../packages/tricycle-sar/README.md#实验记录与归档)。

具体实验报告、成绩表、图表和复现记录保存在 Git 忽略的 `reports/` 或 `artifacts/`，不放入本文或源码提交。原始回执和运行日志留在 `runs/`。离线绘图使用 [analysis/](../analysis/README.md)，结果包不复制执行或绘图源码。
