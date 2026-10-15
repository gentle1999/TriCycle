---
name: sampling-strategy
description: "Select the next batch of reaction candidates with a registered, auditable sampling strategy."
---

# 采样策略与协议

## 适用场景

探索流程已经准备候选池、已有历史和当前模型预测，需要选择下一批计算时使用。本 skill 维护选样边界；多轮执行与恢复见[统一实验指南](../../../docs/mae-intelligent-sampling.md)，一次 TOML 能力见[candidate-select](../candidate-select/SKILL.md)。

## 数据准备

从数据库更新候选时先使用[导出与底物适配](../reaction-db-export-rxnresid/SKILL.md)，按[数据适配规范](../../../docs/reaction-dataset-adaptation.md)冻结数据 hash、身份与匹配策略。保留完整未采池、唯一 ID、当前预测和已揭示有效标签历史。

先验结构与路线数可以提前提供；未来真实标签、计算成败、真实耗时、GT 指标和未采残差不能参与采集。代理与 GT 结构重叠不自动构成标签泄露，仍须区分标签来源及使用范围。

## 选择比较集合

当前文献 profile 为 `LITERATURE_BASELINE_STRATEGIES` 的五个规范 ID：predictive-uncertainty、latin-hypercube、ucb、distance-uncertainty-product、uncertainty-kriging-believer。每条线绑定不同主引用，定义在 `packages/tricycle-sampling/src/tricycle_sampling/protocols.py`；具体文章和证据见[方法清单](../../../docs/sampling-baselines.md)。random/epistemic/aleatoric 是独立策略，未进入该主比较。

新匹配实验使用[六方法套件配置](../../../configs/dpa-unified-sampling-suite.toml)：五条文献基线与 intelligent 智能策略，固定共同池、初始集合、主训练、评价、seed、批量及核时口径。intelligent 以覆盖与成本的初始化提案为起点，利用已揭示反馈调整规则，并进行一次批次复核。需要编写表达式时遵循[智能采集 skill](../agent-authored-acquisition/SKILL.md)。不要将工程覆盖策略或自定成本公式直接归于五篇文章。

## 执行流程

1. 核验候选完整性、ID、字段来源、budget 和 seed。
2. 用 `SamplingRequest` 和 `SamplingPlugin.plan("select", ...)` 生成计划。
3. 调用 `select`；保存完整 selected/remaining、参数、来源和分数或批内步骤。
4. 下游计算仅使用已接受的 selected ID，并保留到实际输入的映射；选择组件不提交任务。
5. 真实标签返回后按共同训练流程更新，再准备下一轮输入；不重做已完成选择。

共同模型流程调用 `tricycle_sar.get_backend("rxnresid")` 的 train/refit/infer 接口，逐轮数据与
checkpoint 更新交给 `tricycle_sar.rxnresid`。新 worker 使用 `tricycle_sar.sampling_runtime.stage`
按[当前后端配置](../../../packages/tricycle-sar/src/tricycle_sar/backends/catalog.json)准备源码与环境入口，
绑定 `sar_backend` 并在执行/恢复前校验；版本与校验值读取配置及其资产清单，不写入技能。

统一基线使用 `experiment_sampling_request()` / `select_experiment_candidates()`，schema `tricycle.matched-sampling/v1`。共同输入为六维 `count/(1+count)` 结构描述符、先验路线的均值与 predictive/epistemic 方差之和、已揭示有效标签参考表。细节以[包接口](../../../packages/tricycle-sampling/README.md)为准。

## 策略关键约束

- Predictive 按总体方差降序；通用 uncertainty API 按 `*_variance`、`*_std`、`*_uncertainty` 的顺序读取明确量纲的分数；也可由 `posterior_probabilities` 推导分类熵。使用规范字段，拒绝旧拼写和嵌套分数表。回归适配不能写成分类熵的完整复现。
- LHS 显式冻结 seed、稠密表征、bounds、design_size；跨轮只推进 offset 并移除已选候选。离散映射和批次前缀不保证严格 Latin 分层。
- UCB 使用 Gaussian 收益后验分位数。统一 adapter 将 RxnResid 认知方差转换为路线均值方差 `sum(v_epi)/n²`，固定 0.95 分位数和 minimize 方向；总体预测方差未自动替代它。
- A Score 为最近已标记参考 Euclidean 距离 × 指定方差；本批参考集合固定，不能改为加权和后保留同一版本。
- KB 在下一项打分前用预测临时标签更新会话。统一 adapter 使用 RxnResid 边际预测及长度尺度为 1 的结构 RBF 假设，不冒称 RxnResid 原生联合后验；禁止退化成静态 top-k 或将临时标签写入正式训练。

独立 API 参数位于 `metadata.baseline`；[TOML 示例](../../../configs/sampling-baselines)用于接口演示，不等同于完整匹配实验配置。KB 可显式注入本地 factory；adapter ID/version、seed、失败和开销需记录。计划阶段仅校验，不创建会话或 refit。

## 记录与失败处理

五项同分均按 ID 升序。插件生成 `sampling_protocol` 和 `sampling_details`，统一 adapter 生成 `sampling_experiment`；保留协议/实现版本、DOI、规则、适配、共同输入 hash 和完整决策记录。相同快照切换策略时共同输入 hash 应一致；轨迹历史分化后不要求它相同。

当前 random 先规范排序再局部抽样，first-n 保留输入顺序；已有实验保留其选择记录，不加载旧实现重建。请求与实验清单只使用注册的规范 ID；旧策略别名直接报错。

缺失预测、无效参数或 hash 不匹配应停止并修正原因，不能随机回退、读未来真值或刷新 hash 放行。恢复保留既有选择、模型链及远程回执。新增采样规则不授权修改 SAR 训练协议、镜像、Torch/CUDA 或历史实验定义。

逐轮固定资产通过项目级 `asset_store` 共享，RxnResid staging 只包含所选模型后端。
每轮记录输入引用及 hash，完整交付由 Bohr adapter 临时组装；操作与恢复见 exploration-campaign
的共享资产说明。资产轻量化不改变训练、先验路线、评价隔离或 checkpoint 继承规则。
