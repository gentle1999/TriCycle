---
name: agent-authored-acquisition
description: "Analyze reaction structures with RDKit, exploration history, estimated cost, and SAR model quality, then author and record a deterministic acquisition function for one sampling round."
---

# 智能体编写采集函数

## 先确定本轮协议

读取请求的 `metadata.agent_acquisition.schema`，或 replay 的 `task.json`、`response-schema.json` 和实验配置，不把某一次实验配方当作本技能的全局默认值。

replay 的 `decision_protocol` 拒绝未知字段。`feedback`、`review` 使用布尔值；核时评价预算与 `guidance` 只用于核时协议。`objective` 由协议名称确定，持久化配置中的值必须一致。遇到拼写或协议冲突时修正请求，不静默关闭反馈或复核。

| 场景 | 响应与科学约束 |
| --- | --- |
| 通用 `exploration.round`，`tricycle.agent-acquisition/v1` | 表达式必须引用新颖度、分布、模型指标与成本；成本必须引用产物数和平均产物原子数 |
| `autonomous-mae-v1` / `autonomous-core-hours-mae-v1`，采集 schema v2 | 可 retain/adjust/replace，按任务 schema 返回七个科学字段；核时模式使用校准后的核时成本，不套用 v1 的强制特征引用 |
| 未配置上述自主协议的 replay 紧凑决策 | 按生成的 response schema 返回公式、成本、理由与分析；输入摘要格式版本不等于采集协议版本 |

通用 QM 轮次由 `exploration.round` 记录；历史标签回放由包内 `tricycle_sampling.replay.selection.select_round` 记录。它们共用采样与证据校验，不反向依赖另一套控制器。回放控制器已生成并校验结构证据时，模型审阅交付材料，无需再次执行结构工具。

## 智能采样策略：cost-aware

匹配套件比较五条文献基线与 cost-aware 智能采样。cost-aware 使用
`autonomous-core-hours-mae-v1` 的核时目标，显式增加
`guidance="coverage-cost-guided-v1"`，feedback/review 均为 true。

初始化提案结合覆盖缺口、池内稀疏度和预计成本：
`(0.5*sampled_novelty+0.5*(1-pool_local_density))/max(1,c/median_pool(c))`，
c 为 product_count×mean_product_atom_count。完整合法表达式、中位数上下文、参考批次和
预计核时由程序写入 acquisition-guidance.json；核时只用已揭示耗时校准。
首轮优先采用参照或提出有依据的调整，之后保留有效规则或根据反馈调整权重、成本敏感度、
区域/不确定性项。偏离参照和前轮规则须说明覆盖与单位核时收益的取舍，
不要默认回到纯最大不确定性，也不要为生成新公式而更换有效规则。

使用固定辅助面板变化、新揭示标签的更新前残差和区域支持数；最多一次科学复核，
检查实际提案与参照在新颖度、稀疏度、预计核时、覆盖和选样重合上的差异。
参照是建议，不强制固定权重，不覆盖智能体最终排序。性能评价不作为逐轮输入；
未知真实标签、未来费用和 GT 指标仍隔离。目标是在全部预设核时预算下优于五条静态基线；不足完整配对覆盖或未达标时如实报告，不能用批次诊断证明 MAE 增益。

协议及记录见 [统一实验指南](../../../docs/mae-intelligent-sampling.md)和
[cost_guidance.py](../../../packages/tricycle-sampling/src/tricycle_sampling/replay/cost_guidance.py)。下文科学自由仍适用，
此节增加明确的起点与偏离解释要求，不修改安全 DSL、证据和有界纠错规则。

## 核时预算目标（由实验显式选择）

目标是 **给定累计代理计算核时预算下，最小化独立 ground-truth 评价池的 MAE**。
核时比较配置 `decision_protocol.name = "autonomous-core-hours-mae-v1"`，并预设
`evaluation_budgets_core_hours`；接口目标为 `minimize-ground-truth-mae-at-core-hour-budget`。
每批候选数仅是执行粒度，不是优化预算，也不作为主要评测横轴。

- 成本是资源约束信息：审阅已消耗核时、预算节点、剩余核时及候选预计核时，说明取舍。
  智能体自由制定完整排序，不强制成本惩罚、新颖度项、权重或多样性配额。
- 仅用已采样对的耗时与先验结构工作量校准 `candidate.estimated_core_hours`。
  `cost_expression` 使用这个字段或 `context.core_hour_budget.calibration.core_hours_per_proxy_unit`，
  输出单位为 core-hours；未经校准的结构代理不能冒充核时。
- ground truth 的标签、整体/区域误差、趋势和阈值命中均不进入智能体输入、训练或控制分支。
  已采样代理标签上的辅助验证与更新前残差允许使用，但不能证明 ground-truth 收益。
- 允许 retain；查看同分数量、截断处同分、ID 排序影响、批次结构覆盖与预计核时。
  这些是诊断，不是额外科学先验；“缺少失败证据”不等于原规则高效。
- 主报告仅比较同核时预算下最新的预算内 checkpoint，且实验定义的全部匹配 seed 均有观测覆盖。
  不取最低历史 MAE、不插值、不外推、不按 ground truth 改规则。训练/智能体开销另记。
- 核时节点是评价预算，不承诺未知真实耗时下的硬费用上限；超过预算的 checkpoint
  不能计入该节点。保持实验配置的训练 epochs 和辅助验证配方，避免同时引入其他实验变化。

配置与入口见 [核时目标说明](../../../docs/mae-intelligent-sampling.md)。
核时协议与采样数量协议共用安全 DSL、证据、七字段响应和纠错边界；预算目标由所选协议决定。

## 采样数量预算的 MAE 自主协议

用户以降低 MAE 为最终目标时，新实验可以设置
`decision_protocol.name = "autonomous-mae-v1"`，使用 `tricycle.agent-acquisition/v2`。
此协议使用模型编写完整采集规则，已有轨迹不自动迁移。
自主 MAE 协议不强制引用特定特征，也不要求每轮更换公式；通用 v1 的特征约束仅用于表中对应场景。
两者均要求证据完整、RDKit 结构真实、安全数值 DSL、全池求值、有限分数、正成本和计算终态/下载 barrier。

- 目标是给定采样预算内降低泛化 MAE。成本只记录和管理预算，控制器不强制成本惩罚。
- 智能体选择 `retain`、`adjust` 或 `replace`；retain 必须逐字保留上一轮的 expression/cost_expression。
  首次决策不能 retain。允许选择或舍弃任何可用信号，并说明依据，不为变化而换公式。
- 响应提供 expression、cost_expression、rationale、author_notes、decision_action、hypothesis、expected_effect。
  假设与预期作用应可在后续轮次检查，不能把难样本残差当作已证明的泛化收益。
- 完整可选特征由程序从本轮真实结构和已揭示标签生成；不能编造未编码的化学先验。
  可比较辅助验证使用实验指定的固定初始有标签底物对面板，辅助训练始终排除这些对。
  主模型仍训练于全部已揭示标签，因此辅助误差不等于主模型留出误差；必须记录面板样本数，不能宣称全局代表性。
- feedback 版本额外提供固定面板误差变化、上一轮假设、前一主 checkpoint 在新揭示标签上的更新前残差、
  分组误差和支持数；不读取未采标签、未采真实成本或最终 ground truth。
- review 版本对完整候选池先检验提案，再提供候选批次特征、区域集中度、与上一规则的选样重合、
  代表路线的 RDKit 相似度。最多一次科学复核，保持或修改均允许，没有强制多样性先验。
- 有选择余地时恒定分数被拒绝；若预算要求选入全部剩余候选，控制器校验证据后直接记录选择，
  `decision_source = "controller-all-remaining"`，不调用模型或冒充智能体作了排序决策。
- 每轮尝试上限读取 `max_decision_attempts`（默认三次，包含首次）；review 每次尝试最多提案/复核两次调用。原始提案、复核、错误和最终响应全部归档。
- 准备时将当前 sampling 运行实现与 worker 输入的完整性绑定写入实验记录；实现变化时先核对差异，不悄悄接续旧实验，也不保存历史控制器实现。

并行实验入口与恢复操作见 [MAE 实验说明](../../../docs/mae-intelligent-sampling.md)。

## 适用场景

底物对先验已经展开为完整的可能产物候选池，需要根据已采样集合的覆盖情况、下一批候选的
预计计算成本和当前构效关系模型精度，逐轮制定采集函数时使用。每轮由智能体根据真实轨迹
制定或保留符合所选协议的公式；不要把本能力退化成选择一个固定 uncertainty baseline。

通用 QM 工作流通过 `exploration.round` 记录结果，回放工作流由其控制器记录。本地决策不调用数据库或 Bohr adapter；结构证据由下文工具或回放控制器生成并校验。采集函数只能是安全数值表达式，不能把智能体临时编写的 Python 作为
评分代码执行。采样决策与计算提交属于两个独立阶段：不能在当前计算任务还在运行时制定/提交下一批，
也不能在同一个计算任务中提交新候选。

## 输入准备

### worker 推理结果

为本轮剩余候选准备包内 `tricycle_sampling.acquisition_worker`。先本地交付已验证的 SAR 环境和包：

```bash
uv run --package tricycle-sampling --extra replay python -m tricycle_sampling.acquisition_worker \
  --prepare-bundle /absolute/worker-input
```

该命令生成 `run.sh` 并复用 `tricycle_sar.sampling_runtime`。将当前 checkpoint、剩余候选 CSV、
带真实标签的留出/交叉验证 CSV，以及 `configs/example-agent-acquisition-worker.toml` 中的请求放入输入目录。
按实际列名修改 ID、目标列、单位、文件路径及 `model_snapshot_id`；候选表只包含本轮已选批次之外的候选。
worker 命令复用 `tricycle_sar.get_backend("rxnresid").inference_command`，不另写安装配方或复制 tools 脚本。

Bohr 执行 `bash run.sh agent-acquisition-worker.toml`：共用启动器初始化环境，使用其 Python 运行包内模块，
推理前核验源码及后端绑定。
该任务同时推理候选与验证表，通过 `bohr.compute.plan/submit` 规划和执行；单次原生推理使用 `sar.infer`。
Bohr 请求结构示例见
[`example-agent-acquisition-bohr-request.toml`](../../../configs/example-agent-acquisition-bohr-request.toml)。

worker 会用同一个 checkpoint 分别推理剩余候选空间和验证集。它要求预测结果与输入 ID 一一对应，
候选推理至少包含 `prediction`、`epistemic_variance`、`aleatoric_variance` 和
`predictive_variance`；并从标明为 held-out 或 cross-validation 的有标签验证集计算带单位的 MAE、RMSE。
该脚本只推理和写产物，不选择候选，
也不提交任务。它不会更新 checkpoint；本轮要反映最新标签时，先把训练/微调完成并将实际产出的
checkpoint 作为输入。

将 `outputs/agent-acquisition/` 纳入 SAR worker 的回传文件。主要产物为：

- `candidate-model-signals.csv`：完整剩余候选行、模型预测、不确定度和 `model_snapshot_id`；
- `validation-predictions.csv` 与 `model-metrics.json`：验证预测、目标值、验证集角色和误差指标；
- `worker-report.json`：checkpoint、候选空间、RxnResid 源码树和输出文件的 hash，以及候选 ID
  覆盖数量。

下载并核对 SAR worker 与本轮计算 worker 的结果后，本地分析者按 `candidate_id` 将模型信号带入候选
payload，再编写 `exploration.round` 的本轮采集函数。把 `model-metrics.json` 中的指标填入
`observed_metrics` 和 `context.model_metrics`，把该文件的 SHA-256 填入
`evidence.model_evaluation`；候选信号 CSV 和 worker 报告的 hash 也应保留在候选空间证据或
`author_notes` 中。SAR 推理 worker 必须成功且报告中的候选数量、ID hash、输出 hash 均核对通过；
缺行、worker 失败或报告不完整时不要用部分预测作采集输入。worker 预测绑定请求中的 checkpoint
快照；若它与本轮新计算出的标签并行运行，
预测不包含这些新标签的训练影响，不能误标为更新后的模型快照。

## 化学结构工具分析（每轮必须）

不能仅凭语言模型对 SMILES、图片或化学直觉的印象确定结构新颖度、密度、官能团数量或反应中心变化。
每轮在所有 worker 结果下载并核对后，必须用仓库中的 RDKit 分析工具处理完整的未采候选池和已有标注
结构。工具解析失败或输入候选 ID 不完整时，先修复输入，不得用部分结构特征继续采样。

```bash
uv run --package tricycle-sampling --extra replay python -m tricycle_sampling.structure_analysis \
  --candidates /absolute/path/to/remaining-candidates.csv \
  --labeled /absolute/path/to/all-previously-labeled-reactions.csv \
  --candidate-id-column candidate_id \
  --reaction-smiles-column mapped_reaction_smiles \
  --labeled-id-column candidate_id \
  --labeled-reaction-smiles-column mapped_reaction_smiles \
  --output-dir /absolute/path/to/persistent-round-analysis/structure-analysis
```

按实际列名修改参数。首轮确实没有已标注结构时省略 `--labeled`，并如实记录空历史；工具会将所有候选
的 `sampled_novelty` 设为 1.0，此时它对候选排序没有区分力，不能把它解释成已观测到的覆盖缺口。
每轮使用新的、持久保存的输出目录，不要把证据放在临时目录。

工具输出：

- `structure-features.csv`：保留输入候选字段，并增加规范化反应 SMILES、反应物/产物原子和结构复杂度
  描述符、产物手性中心、可用时的映射成键/断键/键级变化数量、最近已标注候选相似度、
  `sampled_novelty`、待采池局部密度等逐候选数值；
- `structure-analysis.json`：输入文件和工具 hash、RDKit 版本、Morgan 参数、特征定义、ID 覆盖、
  数值分布摘要及输出 hash。

相似度按反应物侧与产物侧 Morgan 指纹 Tanimoto 相似度的均值计算；默认 radius=2、2048 bits、启用
手性。`sampled_novelty = 1 -` 与所有已标注候选中的最大反应相似度；`pool_local_density` 是与其余
未采候选中最多 10 个最近邻的平均相似度。原子映射从规范 SMILES 和指纹中移除，在完整映射可用时单独
用于反应键编辑统计。工具不做互变异构体/电荷归一化，也不推断三维构象或反应能垒；不要把这些结果
过度解释为真实反应性。

将结构特征表按 `candidate_id` 与 `candidate-model-signals.csv` 和完整剩余候选池一对一合并；检查 ID
无重复、无缺失、无多余后，再把表达式需要的数值特征作为数值类型写入每个候选 payload。不得用行号
对齐。v1 函数必须引用工具生成的 `sampled_novelty`，并在它或其他结构特征在候选池中有变化且与本轮目标
相关时，至少再引用一个有区分力的结构特征，例如 `pool_local_density`、产物环数或反应键编辑数。
若特征为常数、无映射或不可用，应在分析中注明并跳过，不要构造虚假的数值。

智能体必须实际检查 `structure-analysis.json` 的汇总和结构特征表，结合候选 ID 查看新颖度/密度高低
区域以及可代表不同局部化学环境的规范化反应 SMILES。将验证集预测按可对齐的结构特征、结构簇或预先
定义的子结构类别分组，检查各组 MAE/RMSE、残差偏差和样本数；只使用训练集和选择用验证集，最终测试
集不能用于调整采集函数。能与输入结构和 ID 对齐时，比较模型误差较大的区域是否也存在覆盖缺口或分布
偏移。若没有足够验证样本支持某个结构类别结论，要记录样本数不足，不做强结论。

如果本轮确实需要工具尚未输出的官能团/反应类别信号，先用 RDKit 子结构匹配或其他确定性化学分析生成
逐候选特征。记录完整 SMARTS/定义、工具和版本、输入/输出 hash，并核查匹配样例的候选 ID 与结构；
随后才能将该数值字段加入函数。不得把未编码的“看起来更活泼/更拥挤”等主观判断直接当作排序分数。

将分析过程保存在 `metadata.agent_acquisition.author_notes`，至少记录：分析工具及版本/hash、输入结构与
模型证据 hash、主要结构分布或覆盖缺口、分组误差与样本数、支持这些结论的候选 ID/结构、选用和舍弃
的信号、函数各项的决策依据、成本估算局限及本轮结论的不确定性。把特征表和 JSON 报告的绝对路径及
SHA-256 放入 `metadata.agent_acquisition.evidence.structure_analysis`。`exploration.round` 会将函数、
上下文、证据和 `author_notes` 写入带 hash 的 `acquisition-function.toml` 与 trace；保留被引用的原始
分析文件以便复核。

## 决策前核对与采集函数

在调用 `exploration.round` 前，读取并核对：

- 上一批计算任务已结束，先通过 `bohr-batch-status` 确认本地 `status.tsv` 中所有 worker 都处于
  终态（`SUCCEEDED` 或 `FAILED`，不能有 RUNNING/PENDING/QUERY_FAILED），随后通过
  `bohr-batch-download` 下载所有 worker 的结果。确认本地 `download-results.tsv` 中每个 worker
  都是 `DOWNLOADED`，且对应目录中的
  `download-manifest.txt` 所列文件都存在。把该报告的绝对路径和 SHA-256 放入
  `metadata.agent_acquisition.evidence.downloaded_results`，把 `status.tsv` 的绝对路径和
  SHA-256 放入 `metadata.agent_acquisition.evidence.calculation_status`。采样组件会在本地重新
  校验这些条件，缺报告、hash 不一致、任务仍运行、worker 未下载或文件缺失时拒绝决策。
- 完整的尚未采样候选池，以及已有轮次的 `candidate-pool.tsv`、
  `selected-candidates.tsv` 和可用标签；记录对应文件 SHA-256。
- 每个候选 payload 中的 `product_count` 和 `mean_product_atom_count`。前者是该底物对
  先验可生成的产物数，后者是这些产物的平均原子数。缺少任一值时先从生成结果计算并放入
  payload；不要用底物原子数默默替代产物原子数。
- 每个候选的 `sampled_novelty`、`pool_local_density` 和映射反应编辑等结构特征，必须来自上面的
  RDKit 工具产物，并按 ID 合并；把 Morgan 参数和新颖度定义写入
  `context.sampled_distribution.novelty_definition`，报告与特征表的 hash 写入
  `evidence.structure_analysis`。
- 当前构效关系模型的 `model_snapshot`、checkpoint hash、验证集精度指标和评估 artifact。
  优先使用与当前模型快照绑定的留出集/交叉验证指标，不要把训练误差当成泛化精度。
- 将至少一个完全相同的模型指标同时放入顶层 `observed_metrics` 和
  `metadata.agent_acquisition.context.model_metrics`；运行时会校验这两份记录一致。
- `metadata.agent_acquisition.context.sampled_distribution`：已采样集合的大小、关键描述符
  分布、覆盖缺口/稀疏区域等摘要。若历史样本确实为空，明确记录空集状态；不能把“没有历史数据”
  解释成已覆盖。无论是否为空，本轮仍须提供已结束计算的本地 status/download 证据。
- `metadata.agent_acquisition.context.model_metrics`：实际模型精度指标及其方向/单位；公式中
  使用什么精度指标，要在 rationale 中说明。

根据历史计算耗时拟合成本关系时，保留拟合数据的 artifact hash 和模型形式。没有校准数据时，
使用透明的成本代理并在 rationale 中标明它不是实际 Bohr 费用或运行时间。v1 策略要求每轮声明
`cost_expression`，且该表达式必须使用 `product_count` 和 `mean_product_atom_count`；例如
`product_count * mean_product_atom_count` 代表线性工作量代理。模型或历史数据支持时，智能体
可以选择并说明其他显式关系。

## 编写与执行

1. 先完成上一批的状态检查和本地下载；如果还有 worker 在运行或有结果未下载，暂停本轮决策，
   先完成 `bohr-batch-status` / `bohr-batch-download` 流程。
2. 从已下载结果更新标签、采样历史和模型评估；运行 RDKit 结构分析工具并审阅完整报告，汇总已采样与
   待采结构分布。将验证残差按结构特征/局部结构区域分组，结合样本数识别可靠的误差热点和覆盖缺口。
3. 对全部待采样候选使用本轮 `cost_expression` 估计成本，检查总量、中位数、90 分位数和最大值。
   执行时这些统计由采样组件重算并写入轨迹。
4. 结合验证误差与分组样本数、候选预测/不确定度、RDKit 结构特征、覆盖情况和预计成本，为本轮编写
   `expression`。v1 公式必须利用候选级 `sampled_novelty`、`context.sampled_distribution`、
   `context.model_metrics` 和 `estimated_cost`，并在有区分力且相关时再利用结构特征。说明每个信号
   如何改变候选相对排序；避免只乘以对所有候选相同的全局常数。按本轮证据和已观察到的采样收益调整
   公式；自主 MAE 协议允许有依据地 retain，不强制换公式或套用 v1 的特征约束。
5. 将函数、简短 rationale、完整分析摘要、上下文摘要和证据 hash 放入请求的
   `metadata.agent_acquisition`，再运行：

```bash
uv run --package tricycle-agent tricycle-agent run \
  exploration.round configs/example-agent-authored-exploration-round-request.toml \
  --run-id adaptive-round-001
```

该命令只在本地选择并记录候选，不会提交计算任务。下一步若要执行所选候选，另起独立的
Bohr compute/batch-submit 请求；等那批计算结束、下载到本地后，再做下一轮智能体决策。

## 函数格式

采集函数是 `tricycle.safe-numeric-expression/v1` 数值表达式，不是可执行 Python 文件。可以用
候选 payload 的数值字段、`candidate.field`、`candidate["field"]`、`context` 的嵌套字段、
`estimated_cost`，以及 `+ - * / // % **`、比较/条件表达式和以下纯函数：
`abs`、`exp`、`log`、`log1p`、`max`、`min`、`sqrt`。公式输出必须是有限数值；按分数降序选择，
同分按 `candidate_id` 升序打破。

示例公式（每轮必须按本轮证据重新判断，不要求沿用）：

```text
(epistemic_variance * sampled_novelty
    + context.model_metrics.validation_mae
      * (1 - pool_local_density)
      * context.sampled_distribution.coverage_gap) / estimated_cost
```

以下是通用 v1 请求片段；自主 MAE 与 staged-cost 请求使用本轮控制器交付的 schema，不照搬此片段：

```toml
strategy = "agent-authored-acquisition"

[metadata.agent_acquisition]
schema = "tricycle.agent-acquisition/v1"
function_id = "adaptive-round-0001"
version = "1.0.0"
authored_by = "agent-authored-acquisition"
expression = "(epistemic_variance * sampled_novelty + context.model_metrics.validation_mae * (1 - pool_local_density) * context.sampled_distribution.coverage_gap) / estimated_cost"
cost_expression = "product_count * mean_product_atom_count"
rationale = "示例：根据验证误差、新颖度和局部稀疏度分配探索分数，再按预计成本归一化。真实决策必须按本轮证据重写。"
author_notes = "记录本轮结构工具版本与 hash、结构分布、结构分组验证误差、代表性候选 ID、信号取舍和成本代理局限。"

[metadata.agent_acquisition.context.sampled_distribution]
sampled_count = 12
coverage_gap = 0.35
novelty_definition = "1-minus-max-mean-reactant-product-Morgan-Tanimoto-radius2-2048-chiral-v1"
structure_analysis_source = "sha256:<structure-analysis.json SHA-256>"

[metadata.agent_acquisition.context.model_metrics]
validation_mae = 0.21

[metadata.agent_acquisition.evidence]
sampled_set = "sha256:<selected-data-hash>"
candidate_space = "sha256:<candidate-pool-hash>"
model_evaluation = "sha256:<evaluation-artifact-hash>"

[metadata.agent_acquisition.evidence.structure_analysis]
feature_table_path = "/absolute/path/to/structure-features.csv"
feature_table_sha256 = "<structure-features.csv SHA-256>"
analysis_report_path = "/absolute/path/to/structure-analysis.json"
analysis_report_sha256 = "<structure-analysis.json SHA-256>"

[metadata.agent_acquisition.evidence.calculation_status]
manifest_path = "/absolute/path/to/run/status.tsv"
sha256 = "<status.tsv SHA-256>"

[metadata.agent_acquisition.evidence.downloaded_results]
manifest_path = "/absolute/path/to/run/download-results.tsv"
sha256 = "<download-results.tsv SHA-256>"
```

实际配置应保留原始指标、结构分布与误差摘要、成本代理依据、结构工具产物路径/hash 和分析过程。
不要照抄示例中的数值或 rationale。

## 可复现性和约束

- `exploration.round` 必须提供 `model_snapshot.snapshot_id`；完整候选 payload、模型观测指标、
  seed、采集函数表达式、上下文、成本统计和证据都会进入本轮 provenance。
- 每次决策必须引用已下载到本地且校验通过的批次报告。采样策略没有 Bohr submit/status/download
  adapter，也不能与 QM worker 生命周期放进同一运行任务。
- `rounds/NNNN/acquisition-function.toml` 保存智能体公式、上下文、证据、成本统计及函数 hash；
  `model-scores.tsv`、`selection-decisions.tsv`、`selected-candidates.tsv` 保存分数和逐候选成本。
  artifact manifest 与 trace 都覆盖该文件。
- 公式解析和求值由采样包内当前安全解释器完成；禁止 import、文件/网络访问、任意函数调用、
  随机数、系统时间或外部状态。表达式字段错误、成本非正或分数非有限时整轮失败。
- 每个候选都必须提供 `[0, 1]` 的 `sampled_novelty`；v1 要求公式实际引用该字段、模型指标和采样分布。自主 MAE 协议保留证据要求，但不强制采用这些信号。
- 修改后续轮次的函数时，使用新的 `function_id` 或 version。不要覆盖已完成轮次；恢复只能用相同
  请求 hash 和通过 manifest hash 校验的 `resume = true`。

## 历史回放专用 worker

新的历史回放轨迹使用 `packages/tricycle-sampling/src/tricycle_sampling/replay/worker.py`：主模型训练于全部已揭示标签，
辅助验证模型独立从头训练并排除请求中的 `validation_pair_ids` 固定面板；未指定面板时才按 worker 的确定性五分之一划分留出。`model-metrics.json` 明确记录辅助 checkpoint
hash、训练/验证 pair 清单和指标归属；不要声称这些是主模型的留出误差。
剩余候选信号来自主模型，按底物对聚合全部先验枚举产物的不确定度之和。
本地 `packages/tricycle-sampling/src/tricycle_sampling/replay/campaign.py` 负责核验完整覆盖、执行 RDKit、生成验证分组误差、整理
本轨迹采样历史，并生成仅包含采样证据的 decision 目录。agent 只返回公式与分析，
不接触 ground truth 评价文件，不提交 Bohr 任务。详细约定见 `docs/intelligent-replay.md`。

## 简明决策输出

每轮 `rationale` 使用 2–4 句简洁中文（建议不超过 180 字），说明本轮优先什么、依据什么、
如何平衡成本及关键局限。候选 ID、结构样例、分组误差、系数依据和 hash 放在 `author_notes`。
历史回放监督器自动将 rationale、完整公式、成本表达式、选中数量和原始证据链接写入
每轮 `decision/ACQUISITION.md`，并更新轨迹和实验级 `ACQUISITION.md`；不需要另外调用模型摘要。

### 回放决策材料与响应

控制器对完整候选池运行 RDKit，核验 evidence hash、候选唯一性与未采集合、结构路径覆盖和特征聚合，
生成当前 `tricycle.replay-decision-brief/v2` 格式的 `decision-brief.json`。材料包括全池特征与成本分布、
低中高代表结构、特征相关系数、环数组覆盖、验证分组误差与支持数、最差验证样本的误差/结构、
验证误差变化、探索与不确定度高分位交集及最近三轮采集公式。代表结构不是预筛选，
最终公式仍作用于全部未采候选；模型审阅交付材料，无需重跑分析或读取完整原始表。

响应以本轮 `response-schema.json` 为准：基础决策返回 expression、cost_expression、rationale、author_notes；
自主 MAE 还要求 decision_action、hypothesis、expected_effect。材料格式不改变所选采集协议。
模型与推理强度读取 `decision_agent`；调用关闭 shell/unified_exec/sleep 和 web search，
不允许工具调用、读取日志、抄录 provenance 或生成/核算 hash。控制器在进程返回后核对原任务并绑定 hash，
分别保留原始输出与带绑定的 response，完成全池安全 DSL 校验和确定性选择。

单次调用限时为 `agent_timeout_seconds` 与 600 秒中的较小值；每轮尝试上限读取
`max_decision_attempts`（默认三次，包含首次）。超时或无效输出保留失败记录并反馈具体错误。
`decision-input.json` 保存提示词、材料 hash、准备耗时和字符数；`agent-exit.json` 记录调用耗时；
`decision-validation.json` 记录全池校验耗时。完整原始候选与证据保留，已完成的选样不因实现更新重做。
