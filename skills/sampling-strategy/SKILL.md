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
3. 选择已注册策略；内置提供 `random`、`first-n`、`predictive-uncertainty`、
   `epistemic-uncertainty` 和 `aleatoric-uncertainty`。
4. 调用 `select`，保存 selected/remaining 和策略 metadata。
5. 将 selected candidate 交给 ReactionOpt/Bohr 流程，并保留本轮 seed。

## 输出与约束

- 输出 `SamplingResult`，其中 selected 与 remaining 保持候选对象完整性；
- 相同输入和 seed 的内置 random 策略应可复现；
- 三种 uncertainty 策略按对应 payload 分数降序选择候选，并稳定保留相同分数的输入顺序；
- uncertainty 分数优先使用论文释出结果中的对应字段 `predictive_unc`、`epistemic_unc` 或
  `aleatoric_unc`，也接受 `*_uncertainty` 完整拼写；
- 候选也可以提供 `posterior_probabilities`，由采样模块按二分类熵分解纯函数计算三种分数；
- 不在策略插件内偷偷查询数据库或模型；外部信息通过 payload/adapter 显式传入；
- 论文中的 `predictive_unc_based`、`epistemic_unc_based`、`aleatoric_unc_based` 和
  `random_selection` 是兼容别名；新增策略时必须保留稳定策略名和版本。

实现入口：`packages/tricycle-sampling/src/tricycle_sampling/plugin.py`。

## 论文四种基线

对于后验模型样本给出的 `p_i = P(y=1 | x, w_i)`，使用自然对数熵：

```text
predictive = H(mean(p_i))
aleatoric  = mean(H(p_i))
epistemic  = predictive - aleatoric
```

三种 uncertainty baseline 都选择分数最高的候选：predictive 表示总体预测不确定，
epistemic 表示模型知识不足/模型间分歧，aleatoric 表示数据或反应自身的不可约噪声；
`random` 是不使用模型信息的可复现控制组。模型推理和分数生成应由上游显式 adapter 完成，
采样组件只负责排序和选择。
