# 项目文档

本目录维护项目架构、插件接口、数据与实验协议、操作和恢复流程。具体运行状态保存在本地 run，正式结果另行归档。

## 采样与模型实验

- [采样回放入口](intelligent-replay.md)：选择逐轮套件、整条 worker 或单次本地选样入口。
- [统一采样实验](mae-intelligent-sampling.md)：共同实验条件、五基线与智能策略、预算评价、执行和恢复。
- [采样基线方法与文章](sampling-baselines.md)：五篇不同主引用、使用证据及 TriCycle 适配边界。
- [采样包接口](../packages/tricycle-sampling/README.md)：请求字段、方差转换、KB adapter 与审计记录。
- [冻结采样空间](frozen-sampling-space.md)与[数据适配](reaction-dataset-adaptation.md)。
- [构效关系基线](model-baselines.md)、[SAR 包](../packages/tricycle-sar/README.md)与[SAR 加速器执行约定](sar-accelerator-execution.md)。

## 架构与集成

- [当前架构与设计边界](architecture.md)。
- [插件契约](plugin-contract.md)与[开发说明](development.md)。
- [Bohr 模型运行环境](bohr-dl.md)。
- [反应数据库集成](reaction-db.md)。
- [余额告警工具](balance-alert.md)。

## 研究与运行归档

默认 run 位于 `runs/YYYY-MM-DD/HH-MM-SS.ffffff±ZZZZ/<run-id>`，日期和时间使用本机时区；显式目录及恢复路径按请求保留。运行状态、凭据相关材料、机器路径和监督日志不放入通用指南。

本地研究报告、正式结果包、图表和复现记录位于 Git 忽略的 `reports/` 或 `artifacts/`，不随源码提交。过时文档按原字节移入 `artifacts/document-archive/` 并记录原路径、哈希与替代入口；清理后指南和测试不依赖本机归档。

SAR 五折训练方法由包内 [backends/catalog.json](../packages/tricycle-sar/src/tricycle_sar/backends/catalog.json)组合共享的环境初始化和训练/推理入口，[锁文件](../configs/sar-training-protocols.lock.json)作哈希绑定引用。只维护当前方法和源码；结果核验独立于执行源码，重跑在新目录中使用当前后端。

离线统计分析、SAR 与采样绘图及其依赖锁集中在 [`analysis/`](../analysis/README.md)。模型包提供运行和结果校验接口，分析层读取这些接口；`packages/` 不导入分析层。
