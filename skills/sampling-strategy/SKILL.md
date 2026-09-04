---
name: sampling-strategy
description: "Select the next batch of reaction candidates with a registered, auditable sampling strategy."
---

# 采样策略

## 适用场景

当探索循环需要从尚未计算的候选反应空间中选择下一批任务时使用。

## 输入

- 唯一的 candidate ID 和可选 payload/features；
- `budget`、策略名和可复现的 `seed`；
- 可选的模型预测、不确定性、历史状态等 metadata。

## 工作流

1. 用 `SamplingRequest` 检查候选 ID 唯一性和 budget 边界。
2. 用 `SamplingPlugin.plan("select", ...)` 先生成可审计计划。
3. 选择已注册策略；当前骨架提供 `random` 和 `first-n`。
4. 调用 `select`，保存 selected/remaining 和策略 metadata。
5. 将 selected candidate 交给 ReactionOpt/Bohr 流程，并保留本轮 seed。

## 输出与约束

- 输出 `SamplingResult`，其中 selected 与 remaining 保持候选对象完整性；
- 相同输入和 seed 的内置 random 策略应可复现；
- 不在策略插件内偷偷查询数据库或模型；外部信息通过 payload/adapter 显式传入；
- 后续增加 uncertainty、diversity、MSOS 等策略时，必须保留策略名和版本。

实现入口：`packages/tricycle-sampling/src/tricycle_sampling/plugin.py`。

