# 采样回放入口

TriCycle 用历史计算结果构建新的主动学习轨迹：先选择底物对，再揭示该对可用的代理标签，更新本轨迹 RxnResid，最后在冻结 ground truth 上评价。采样侧只获得先验结构、当前预测和已揭示历史。未来真实标签、计算成败、真实核时和 GT 指标留在回放及评价侧。

比较集合由五条文献基线与 `cost-aware` 智能策略组成。cost-aware 融合结构覆盖、成本、模型反馈和批次复核。各方法共用候选池、匹配初始集、主模型训练、固定评价和预算口径；随机只用于共享初始化，未作为新比较曲线。

## 选择入口

| 需求 | 入口 | 配置或文档 |
| --- | --- | --- |
| 准备、推进多方法逐轮比较 | `tricycle_sampling.replay.suite`，复用 `tricycle_sampling.replay.campaign` | [统一套件配置](../configs/dpa-unified-sampling-suite.toml)、[实验与恢复指南](mae-intelligent-sampling.md) |
| 每个远程 worker 执行一条完整基线轨迹 | `tricycle_sampling.active_learning` | [五基线配置](../configs/rxnresid-matched-sampling.toml) |
| 本地选择一次候选批次 | `candidate.select` / `SamplingPlugin` | [采样包接口](../packages/tricycle-sampling/README.md)、[独立 TOML 示例](../configs/sampling-baselines/) |
| 查看方法来源及原文使用证据 | 版本化文献协议与文献登记 | [方法与文章清单](sampling-baselines.md) |
| 核验固定候选空间 | 冻结数据与底物身份适配流程 | [采样空间](frozen-sampling-space.md)、[数据适配](reaction-dataset-adaptation.md) |

两个基线执行入口共同调用采样包的 `select_experiment_candidates()`，协议为 `tricycle.matched-sampling/v1`。逐轮套件通过 cost-aware 执行智能采样。共享的是采样与主实验定义；不同入口的调度粒度和产物布局仍各自记录，不能仅凭接口一致合并结果。

## 新实验准备

```bash
uv run --package tricycle-agent python -m tricycle_sampling.replay.suite prepare \
  --config configs/dpa-unified-sampling-suite.toml
```

准备仅创建本地套件、配置、冻结记录和初始 worker 输入。配置引用已有本地数据与先验结构，实际路径和 hash 必须核验；缺少输入时不会自动下载或替换。远程执行需已有实验授权，并显式使用 live/submit 开关，详见[操作指南](mae-intelligent-sampling.md)。

每轮沿共同流程执行：标签揭示 → 本轨迹训练/推断 → 固定评价 → 结构与采集输入准备 → 策略选择 → 下一轮。五条基线使用预先固定规则；cost-aware 根据覆盖、成本和模型反馈制定表达式并复核实际批次。Bayes-UCB 使用 RxnResid 认知方差的 Gaussian 近似，KB 增加明确记录的结构 RBF 相关性假设和批内条件化。

## 运行记录与归档

套件必须显式配置 `sampling_strategies`。通用候选选择 API 的随机与不确定度方法见采样包文档。已有实验记录保留，当前代码不自动迁移配置。源码、配置、数据或 worker hash 不一致时，严格运行检查会拒绝接续；使用当前 SAR 后端创建新实验，不能刷新既有 run 的 hash 放行。

具体 run 的状态、机器路径、提交回执、日志和临时恢复记录保存在 `runs/`；正式结果整理为 Git 忽略的 `reports/` 或 `artifacts/` 中的独立结果包，不随源码提交。本文及相关指南维护通用协议，不把某次运行的实际数量或完成状态写成所有实验的约束。
