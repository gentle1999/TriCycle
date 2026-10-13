# 统一采样实验：基线、智能策略与执行

新实验统一候选池、匹配初始集、RxnResid 主模型、训练与 checkpoint 更新、评价和预算口径，变化限定在选样策略。主配置为 [dpa-unified-sampling-suite.toml](../configs/dpa-unified-sampling-suite.toml)，包含五条文献基线和智能采样策略 `cost-aware`；套件标识从配置读取。本文维护协议和操作流程；具体 run 的数量、状态和恢复证据保存在其运行目录。

方法来源见[文献清单](sampling-baselines.md)，请求、方差公式与 adapter 见[采样包接口](../packages/tricycle-sampling/README.md)。

## 方法集合与共同条件

| 方法 | 选择规则或策略差异 |
| --- | --- |
| `predictive-uncertainty` | 路线总体预测方差之和降序 |
| `latin-hypercube` | 冻结 LHS 序列，在共同结构空间映射候选 |
| `ucb` | RxnResid Gaussian Bayes-UCB，固定 0.95 分位数，偏好较低预测能量 |
| `distance-uncertainty-product` | 到已揭示标签集合的最近距离 × 路线总体方差之和 |
| `uncertainty-kriging-believer` | 认知标准差逐个选样，结构 RBF 后验批内条件化 |
| `cost-aware` | 根据结构覆盖、模型反馈和预计成本动态制定规则，并进行一次批次复核 |

cost-aware 的参照、反馈和复核属于同一智能采样策略定义。所有方法使用相同主模型和评价；五条基线不调用决策模型。通用候选 API 另提供随机、认知及偶然不确定度选择；本套件按显式方法列表生成曲线。

逐轮 `replay.suite` 接口要求五个匹配初始集合、每条轨迹 40 个评价轮次、每批 40 对，并强制共同 300 epochs 配方；这些是当前控制器的协议约束，不是某次运行的完成统计。对应 seed 的各方法初始 ID 完全相同，采集 seed 从配置读取。新轨迹从初始标签重新训练，后续只延续自己的 checkpoint，不借用其他策略模型。

准备核对 `initial_pair_ids`、`validation_panels`、`evaluation_sha256`、`training`、`source_revision`、`sar_backend` 及 `shared_protocol` 一致。后者绑定匹配采样 schema、固定 seed、轮数、批量、私有数据和 assets hash。方法分化后的已采历史、训练集和预测会自然不同；公平性要求输入口径和更新规则相同，不要求所有轮次预测相同。

数据来源与初始集合由输入绑定；控制器的固定训练与轨迹约束见上文，不能仅修改 TOML 就视为换用另一协议。SAR 五折基准使用包内 [backends/catalog.json](../packages/tricycle-sar/src/tricycle_sar/backends/catalog.json)组合共享的环境初始化和训练/推理入口；采样策略复用同一后端；重跑统一使用当前后端并记录实际方法绑定。

采样模型调用直接复用 SAR 的 `get_backend("rxnresid")`：首轮调用 `training_command`，后续调用
`refit_command`，预测调用 `inference_command`。逐轮配置、已揭示标签 split、checkpoint 数据更新
及冻结 route 词表由 [SAR RxnResid adapter](../packages/tricycle-sar/src/tricycle_sar/rxnresid.py)
统一提供。采样层传入当前数据，不另构造模型命令或环境安装配方。

新准备任务使用 [SAR sampling runtime](../packages/tricycle-sar/src/tricycle_sar/sampling_runtime.py)
交付当前模型资产指定并经校验的源码归档，并调用原 SAR 初始化入口继承镜像环境。
`sar_backend` 绑定 method ID、源码 revision、环境资产与共享接口 hash；worker 执行前验证，
恢复时核对上一 checkpoint 的同一绑定。版本/hash 不符即停止，不能回退旧源码或刷新 hash 放行。

## 智能采样策略：cost-aware

cost-aware 将结构覆盖、预计成本、标签反馈与提案复核合并为一个决策流程，目标是在相同核时预算下优于全部五条静态基线。协议主体仍为 `autonomous-core-hours-mae-v1`，增加 `guidance="coverage-cost-guided-v1"`，同时启用 feedback/review；引导协议版本为 1.0.0，定义见 [cost_guidance.py](../packages/tricycle-sampling/src/tricycle_sampling/replay/cost_guidance.py)。它不增加主模型或改变共同训练/评价。

每轮程序在完整未采池重算已采新颖度、局部稀疏度和结构工作量，生成智能策略的初始化提案：

```text
c = product_count * mean_product_atom_count
initial_score = (0.5 * sampled_novelty + 0.5 * (1 - pool_local_density))
                  / max(1, c / median_pool(c))
cost_expression = estimated_core_hours
```

中位数使用全池统计并写入 `context.coverage_cost.cost_proxy_median`；预计核时依已揭示耗时校准。首个采集决策优先采用参照公式，可根据当轮证据提出具体调整；首轮 action 为 replace，之后允许 retain/adjust/replace。智能体可调整覆盖权重、成本敏感度，或加入有支持数的区域误差/不确定性项，偏离参照时应说明取舍。参照是引导和比较依据，不由控制器覆盖智能体排序，也不强制固定权重。

反馈来自固定辅助面板、上一轮假设、新标签更新前残差与区域支持数。提案在全池执行后，与参照批次比较新颖度、稀疏度、预计核时、同分、底物覆盖和选样重合；随后进行最多一次科学复核。提高成本或降低覆盖时应解释预计收益，不能只因为有方差字段便退回纯最大不确定性，也不为制造新公式而换规则。上一轮公式在缩小后的池上成为常数时，仅按 ID 同分规则记录反事实诊断，不阻塞一个已通过校验的新提案。

`acquisition-guidance.json` 保存版本、默认公式、当前输入 hash、全池参考排名、参照 ID 和诊断；其摘要进入决策材料。`formula-assessment.json` 与 `proposal-review.json` 保存实际提案与参照差异，智能表达式审计会独立重算该参照。不存在未执行批次的 MAE 收益，不能将批次诊断当作因果证据。

权重、成本敏感度和模型信号由智能体根据已揭示反馈决定；初始化提案仅用于决策起点和批次诊断，不作为另一条比较曲线。不把 GT 指标送入逐轮采集，最终性能由新的匹配轨迹检验。

## 统一采集输入

两个基线执行入口共用 `select_experiment_candidates()`，协议为 `tricycle.matched-sampling/v1`。五条线接收共同六维结构描述符、同一先验路线集合的预测均值与方差，以及已有有效标签底物对的参考结构；白名单排除目标标签、未来真实成本、未采成功掩码和 GT 指标。

六维为反应物的重原子、杂原子、环、芳香原子、可旋转键和氢键受体计数，变换固定 `count/(1+count)`。各策略的使用方式预先冻结：Predictive 与 A Score 使用路线方差求和；UCB 与 KB 使用路线均值的认知方差 `sum(v_epi)/n²`。

Bayes-UCB 因而可以直接使用 RxnResid 认知方差，不需要另训一个主预测模型。它使用路线独立的 Gaussian 近似，统一方向为低预测能量优先；这与总体 MAE 目标的差异应报告。KB 在相同边际均值/方差上增加长度尺度为 1 的结构 RBF 相关性假设，逐个条件化；相关性并非 RxnResid 原生输出。批内临时标签不进入正式训练数据。

LHS 固定初始批次之后的总设计规模及 seed/bounds，每轮 offset 等于已消耗设计点数；不得根据剩余池重设边界。离散映射和前缀批次不保证严格分层。共同预测缺失时停止补全输入，不静默转 random。

## 标签、评价与辅助反馈

训练只揭示已经选择的底物对的代理标签，失败或无有效标签也在选择之后记录。结构和先验路线可预先给定；未采历史能量、成败、真实计算耗时留在回放私有侧，GT 标签及表现只进入评价侧。

当前协议在同一固定候选空间上评价主模型，因此随着采样推进，评价空间包含越来越多训练底物对。代理能量和 GT 能量来源可以不同；结构重叠本身不等于 GT 标签泄露。主指标是固定候选空间整体 MAE，不能直接解释为全新底物空间的泛化误差。数据行数、标签覆盖与结构重叠应从各 run 的冻结数据另行报告。

每条 seed 从初始已存在有效标签的底物对中按固定 hash 选择八对辅助面板；各方法同 seed 使用相同面板。辅助模型每轮从头训练并排除面板；主模型仍使用全部已揭示标签，保持共同续训规则。八对面板是小规模代理反馈，不能估计全池精度，也不是主模型 GT 留出测试。

新标签更新前残差使用上一 checkpoint 对刚揭示标签的预测，在本轮训练之前计算；它描述所选样本难度，不能证明泛化增益或因果效果。无有效新增标签时记录缺失或按共同规则复用 checkpoint，不伪造残差。评价指标不用于停止、淘汰或切换策略。

## 核时预算与统计

新配置使用 `decision_protocol.name="autonomous-core-hours-mae-v1"`，目标为给定累计代理计算核时下的 GT MAE。评价节点读取 `decision_protocol.evaluation_budgets_core_hours`，不是所有空间的固定额度，也不保证未知实际耗时下整批计算不越界。

在核时节点，对每个 seed 取最新已完成且预算内的 checkpoint，不取历史最优 MAE，不使用超预算点，不外推超过实际覆盖的区间。配对差值必须来自同 seed、同核时预算。主比较需共同 seed 覆盖；监控图可显示未完成轨迹，但必须标明每点可用 seed 数，不能把缺失写为零或宣称仅完成者的均值代表完整实验。

智能策略的候选成本估计仅用已揭示耗时和先验结构代理校准。没有有效校准依据时阻塞，不能借用未来真实费用。成本表达式生成正数估计用于审计；MAE 协议不强制将成本除入采集分数。基线依照冻结规则选择，使用相同核时评价与记账。

`cumulative_sampling_cost_core_hours` 是历史代理量化计算成本，不是新增 SAR 任务账单。SAR 训练/推断秒数和决策模型调用耗时另列；被拒提案也计入调用开销。阶段秒数累计不等于实验历时或 GPU 小时；实际设备、排队、故障和缺失时长另行报告。

最终目标的评价写入 `intelligent-comparison.json`：在所有预设核时节点，智能策略分别与五条静态基线按相同 seed 配对，要求五 seed 的平均 MAE 差值全部小于零。全部节点满足记为 passed；完成评价后持平或未优于任一基线记为 not-met；任一方法缺少完整 seed/成本覆盖则记为 pending。该标准描述平均性能，不等于统计显著性；不足覆盖时不宣称达标。评价结果只用于报告，不回流本轮采集或触发策略切换。

旧 `autonomous-mae-v1` 使用样本预算，保留原配置和评价口径，不能与核时协议混标。相同终点采完全部候选仍可能因训练顺序产生不同精度，保留完整预算轨迹。

## 本地准备

先核验配置引用的本地 readiness、先验结构、冻结标签、评价、模型源码版本与 assets。示例中的路径来自既有归档；缺失时准备失败，不自动替换数据或下载模型。

```bash
uv run --package tricycle-agent python -m tricycle_sampling.replay.suite prepare \
  --config configs/dpa-unified-sampling-suite.toml
uv run --package tricycle-agent python -m tricycle_sampling.replay.suite status \
  --run-dir /absolute/suite
uv run --package tricycle-agent python -m tricycle_sampling.replay.suite tick \
  --run-dir /absolute/suite
```

未给目录时使用本机时区的 `runs/YYYY-MM-DD/HH-MM-SS.ffffff±ZZZZ/<suite-id>`；prepare 可用 `--run-dir` 指定新的空目录。上述默认路径仅本地读写，不读凭据、不访问网络、不调用决策模型或提交任务。

检查 `suite.json`、`suite-hash.json`、`preflight.json` 和各方法的 `experiment.json`。方法集合、轨迹数、任务与并行上限以生成的计划核对；当前控制器下 `M` 种方法对应 `5M` 条轨迹、最多 `200M` 个轮次任务。这些是计划上限，不是实际提交数或费用；任务内部还可能包含主模型与辅助模型拟合。

suite 请求必须显式提供非空且不重复的 `sampling_strategies`，准备时记录匹配 schema；不从省略字段推断实验集合。

## 远程执行与监督

在已有实验执行授权范围内使用：

```bash
uv run --package tricycle-agent python -m tricycle_sampling.replay.suite watch \
  --run-dir /absolute/suite --live --allow-submit
uv run --package tricycle-agent python -m tricycle_sampling.replay.suite service-file \
  --run-dir /absolute/suite --allow-submit
```

`--live` 才启用服务查询、下载和智能决策；`--allow-submit` 才允许后续提交。`service-file` 只生成独立 `supervisor.plist`，安装和启动系统服务需另行执行，不把准备完成写成已启动。

各方法 seed00 先完成三个评价轮次，再开启本方法其余四个 seed；这是执行验证门槛，不按 GT 优劣筛选方法。同一轨迹轮次顺序执行，seed 故障独立隔离。所有任务在共享文件锁下创建并复用一个 Bohr 任务组，提交回执持久保存。节点故障的有限重调度依冻结 job spec 执行，不盲目重试提交 API。

源码、配置、数据、worker、checkpoint 和输入输出 hash 在准备或对应阶段绑定。可变源码和配置的 worker 快照使用独立文件，避免工作区编辑传播到历史归档；不可变大资产可以复用链接。哈希不匹配时停止，不能刷新绑定来继续旧实验。

## 决策与运行产物

| 产物 | 用途 |
| --- | --- |
| `suite.json` / `preflight.json` | 方法清单、共同协议、初始化一致性与任务上限 |
| 各方法 `experiment.json`、各 seed `state.json` | 冻结条件、轨迹阶段、已选集合与暂停/故障状态 |
| `status.json`、`REPORT.md`、`evaluation-curves.json` | 本地总览与已完成评价点；不进入采集输入 |
| 基线 `baseline-ranking.json` / `baseline-selection-request.json` | 确定性策略执行、来源、共同输入、分数或 KB 步骤及请求绑定 |
| 智能 `ACQUISITION.md` 与函数/响应记录 | 表达式、假设、预计作用、上下文和证据 |
| cost-aware 及历史 reviewed 的 proposal/review 响应与批准记录 | 提案、实际批次诊断、最多一次复核和最终绑定 |
| submission、下载清单、worker 输出与 checkpoint | 远程句柄、输入输出完整性及同轨迹模型链 |

智能响应包含 `expression`、`cost_expression`、`rationale`、`author_notes`、`decision_action`、`hypothesis`、`expected_effect`。action 为 retain/adjust/replace；retain 必须逐字保留上一轮两条表达式。有选择余地时拒绝全池恒定分数；全部剩余候选必须选入时记录确定性选择，省去决策模型调用。

每轮最多三次纠错尝试，开启复核的智能策略每次尝试最多一次科学复核；失败记录保留。认证错误单独阻塞，不作为科学错误重试。决策完成前须核验上一轮计算已终态、结果已下载及完整候选覆盖；科学自由不能替代完整性检查。

## 暂停、接续与故障

单方法暂停和恢复使用：

```bash
uv run --package tricycle-agent python -m tricycle_sampling.replay.campaign pause \
  --run-dir /absolute/suite/<variant>
uv run --package tricycle-agent python -m tricycle_sampling.replay.campaign resume \
  --run-dir /absolute/suite/<variant>
```

暂停不终止远端任务。接手先读冻结配置、各 seed 状态、提交回执、进程记录及已有选择；恢复已完成计算和决策，不能重新采样或重复提交。认证修复只恢复尚未完成的决策阶段。监督线程已因 blocked 退出时，修复状态后还需恢复对应监督，不凭锁文件推断进程存活。

**不确定提交：** 超时不证明任务未创建。保留 inflight，按完整任务名、时间、项目和组查询平台并核对近期记录。唯一匹配核验归属后补齐回执，继续监控；多个匹配或查询不完整时继续核对，不清空标记。确证未提交后的一次恢复仍需保持原选样、输入和 job spec，并保存查询及恢复证据。

以下专用工具有明确范围，不能当作所有策略的通用自动修复：

| 工具 | 支持范围与恢复约束 |
| --- | --- |
| `tricycle_sampling.replay.watch` | cost-aware 及历史 autonomous/feedback/reviewed 的单 seed；默认读本地，live 需绑定已归档恢复记录；保留原锁、hash 和提交保护 |
| `tricycle_sampling.replay.recover_review` | reviewed 当前阻塞轮；新提案正常通过预算校验时，允许旧常数公式仅用于明确标注的反事实批次诊断，完成原定一次复核 |
| `tricycle_sampling.replay.recover_retain` | 最后已完成响应明确 retain，且简写与旧表达式在全池分数、正成本及完整排序完全等价；留证后恢复原文本，不增加提案次数 |

专用恢复先默认只读核对，适用时在原授权范围内显式 `--live`。保留原被拒记录、计数及当前实现 hash；不能改变已接受选择或重置纠错上限。基线故障按其缺失输入或确定性执行原因处理，不调用智能公式恢复工具。

只读核对示例：

```bash
uv run --package tricycle-agent python -m tricycle_sampling.replay.watch \
  --run-dir /absolute/suite --variant cost-aware --seed 1
uv run --package tricycle-agent python -m tricycle_sampling.replay.recover_review \
  --decision-dir /absolute/current-blocked-decision
uv run --package tricycle-agent python -m tricycle_sampling.replay.recover_retain \
  --decision-dir /absolute/current-blocked-decision
```

轮次路径必须指向实际阻塞的当前决策。单 seed live 接续还需 `--recovery-record /absolute/archived-blocked-state.json --live --allow-submit`；它不自动清除 blocked。已经推进或完成的轨迹仍须核验归档、选样和 checkpoint 链，再决定是否接续。

## 审计及其范围

五条基线的计划/结果保存 `sampling_protocol`、`sampling_details`、`sampling_experiment`，实验控制器另外绑定源码、输入和选择请求。最终核验应从这些冻结输入按原版本重放相同 selected IDs，并逐轮核对排除集合、真实标签揭示、worker hash、模型链、评价和核时；来源 DOI 本身不能证明执行正确。

现有 `tricycle_sampling.replay.audit` 独立重算智能表达式、cost-aware 默认参照与 worker 生命周期，支持 cost-aware 和原智能采集记录。其 `--require-complete` 检查计划任务及决策覆盖，**尚不能作为新五条基线整套选择的完整独立审计入口**。不要把通用状态检查或部分审计通过写成六方法最终验收。

审计使用当前包内实现；不提供加载历史控制器源码的入口。既有结果与原绑定保留，绑定不匹配时明确拒绝，不改写旧 hash。

平台审计另用 `tricycle_sampling.replay.platform_audit`，默认读 `--snapshot`，显式 `--query` 才查询。需证明列表完整，并核对 canonical Bohr ID、任务名、公共组、重复或额外任务；完整验收按 suite 计划数量，而非固定旧三方法数量。当前查询单页最多 1000 条，计划任务多于一页时可能超出其能力；`has_more=true` 时拒绝认定完整，不能将第一页当作全量。平台审计与本地执行审计不能互相替代。

## 图表与结果归档

具体运行的报告、成绩、图表和复现记录只写入 Git 忽略的 `reports/`、`runs/` 或 `artifacts/`，不纳入维护文档或源码提交。

新统一套件使用支持文献基线的 `analysis.sampling.replay_sampling_panels`，先导出新的固定快照，再在具有 matplotlib 的报告环境渲染：

```bash
uv run --project analysis --locked python -m analysis.sampling.replay_sampling_panels \
  --run-dir /absolute/suite --out-dir /absolute/new-snapshot-directory
uv run --project analysis --locked python -m analysis.sampling.replay_sampling_panels \
  --snapshot /absolute/new-snapshot-directory/snapshot.json \
  --out-dir /absolute/new-snapshot-directory
```

第二条应使用已配置项目依赖及 matplotlib 的解释器。输出 PNG、SVG、PDF 和 `figure-manifest.json`；渲染会核验快照聚合，图表 manifest 绑定快照、渲染源码和输出 hash。已有结果更新另建快照目录。

图按实际出现的代理空间布局：上排 GT MAE，下排同 seed、同成本的配对 MAE 差值。新集合没有 random 时参照 predictive uncertainty；历史快照含 random 时保留原参照。静态虚线、智能实线，误差带为样本标准差，不是置信区间。未完成轨迹保留可用 seed 数，少于五条用细线，单 seed 不画误差带，不外推。

最终报告同时列共同条件、协议/源码/hash、来源与适配、逐 seed 完成度、预算支持区间、成本与运行开销、故障恢复和审计覆盖；无完整验收时明确范围。

## 整条轨迹 worker 入口

只比较五条文献基线并按整条轨迹提交时，使用 [rxnresid-matched-sampling.toml](../configs/rxnresid-matched-sampling.toml)：

```bash
uv run --package tricycle-agent python -m tricycle_sampling.active_learning validate \
  --config configs/rxnresid-matched-sampling.toml
uv run --package tricycle-agent python -m tricycle_sampling.active_learning prepare \
  --config configs/rxnresid-matched-sampling.toml
```

整条轨迹的 seed、轮次、采集 seed 和训练参数读取请求；模型源码读取 SAR 当前资产。可用 `--rxnresid-source` 核验本地 clean checkout，但交付仍采用包内经校验的归档，准备不会 clone 或升级。执行粒度为一 worker 一条轨迹，任务数量、资源和产物不等同于逐轮套件；两者复用相同采集接口与 SAR 后端，合并结果仍需核对数据、训练、评价和执行定义。

已有实验继续保留其配置、绑定与结果，不保留可执行历史实现。新增匹配实验创建新目录和版本；不得用旧轮次结果、不同 epochs 的 checkpoint 或改写 hash 的归档冒充新协议结果。
