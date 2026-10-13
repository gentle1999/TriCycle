---
name: candidate-select
description: "Select the next reaction candidates deterministically from a TOML sampling request."
---

# 选择下一批反应候选

## 适用范围

从显式给定的候选池选择一个批次，并保存策略、来源、参数和 seed。此能力不训练模型、不查询数据库、不提交计算；外部输入由独立 adapter 准备。

需要多轮历史回放时使用[统一实验指南](../../../docs/mae-intelligent-sampling.md)；需要智能体逐轮制定公式时使用[agent-authored-acquisition](../agent-authored-acquisition/SKILL.md)。

## 输入

TOML 包含 `strategy`、非负且不超过候选数的 `budget`、`seed`、`candidates` 表数组和可选 `metadata`。每项有唯一非空 `candidate_id` 和 `payload`。通用接口默认 strategy 为 random；文献比较必须显式指定策略。

最终五项规范 ID 为：

| 策略 | 主引用 DOI |
| --- | --- |
| `predictive-uncertainty` | `10.1038/s41467-025-59812-0` |
| `latin-hypercube` | `10.1126/science.adj1817` |
| `ucb` | `10.1038/s41586-024-07021-y` |
| `distance-uncertainty-product` | `10.1038/s41467-024-49011-8` |
| `uncertainty-kriging-believer` | `10.1021/jacs.5c02218` |

来源以采样包 `protocols.py` 为准，文章与证据见[文献清单](../../../docs/sampling-baselines.md)。`random`、`epistemic-uncertainty`、`aleatoric-uncertainty` 是独立策略，未进入上述文献比较集合。

LHS、UCB、A Score 和 KB 的配置位于 `metadata.baseline`，拒绝未知参数：LHS 需稠密表征、固定边界与整数 seed；UCB 需收益后验均值/标准差、分位数与方向；A Score 需已标记参考结构；KB 需联合后验或显式注入本地 surrogate factory。完整参数及[五项 TOML 示例](../../../configs/sampling-baselines/)见[包接口](../../../packages/tricycle-sampling/README.md)。

## 执行

1. 核对完整未采候选池、ID 唯一性、字段来源、有限数值和预算；数据导出遵循既有身份与冻结 hash。
2. 使用 `SamplingPlugin.plan("select", ...)` 校验并保存计划。KB 计划阶段不创建会话或 refit。
3. 执行选择，保留 selected/remaining、seed、协议、有效参数与决策明细。
4. 将选择结果交给下游显式计算流程；不得把结果写回原请求或覆盖既有选择。

```bash
uv run --package tricycle-agent tricycle-agent run \
  candidate.select configs/sampling-baselines/ucb.toml --run-id sampling-ucb-demo
```

结果自动包含 `metadata.sampling_protocol` 和 `metadata.sampling_details`；请求不能伪造这两个结果字段。五项同分按 candidate ID 升序处理。保存完整输入及源码版本，不仅记录策略名。

## 统一实验规则

新匹配比较通过 `experiment_sampling_request()` / `select_experiment_candidates()`，schema 为 `tricycle.matched-sampling/v1`。五项共享六维固定结构表征、先验路线、RxnResid 预测和已有有效标签参考集合，只切换选择规则。两种基线控制器使用相同 adapter；共同输入 hash 绑定当前轮次快照。

Bayes-UCB 使用认知方差 `sum(v_epi)/n²` 构造路线平均能量的 Gaussian 近似，统一方向 minimize；不将总体预测方差自动替代收益方差。KB 增加固定结构 RBF 相关性，批内逐个条件化，不另训主 SAR，不把临时标签加入正式训练。LHS 固定总设计规模及边界，每轮推进 offset，不从剩余池重设范围。

候选真值、未采成功掩码、未来真实成本和 GT 指标不能进入采集输入。共同预测缺失时补全或停止，不静默回退 random。策略适配不授权修改冻结 SAR 训练协议或刷新 hash 接续旧实验。

## 失败与恢复

候选、预算、策略或后验无效时修正输入并新建本地选择 run。采样本身没有远程提交与恢复阶段；已有多轮轨迹按运行记录核对当前包实现和输入绑定后恢复，保留已接受选择和提交回执，不保存历史控制器源码。
