# tricycle-sampling

`tricycle-sampling` 从显式给定的候选池中选择下一批反应，并提供多轮 SAR 采样回放的准备、监督、决策和审计。模型训练与推断使用 `tricycle-sar` 的共用接口，远程任务通过注入的 Bohr adapter 执行。

## 回放运行入口

整条采样实验的准备、提交和收集使用 `python -m tricycle_sampling.active_learning`。空间快照由 `space_snapshot.py` 校验和生成，只保存数据与适配证据，不复制控制器源码。提交直接调用注入的 Bohr gateway；同一实验共享任务组和提交锁，已有回执复用，提交结果不明时保留标记并拒绝自动重提。

智能采集的独立推理 worker 使用 `python -m tricycle_sampling.acquisition_worker --prepare-bundle <目录>` 交付包及 `run.sh`，随后添加请求、checkpoint 和输入表。Bohr 执行 `bash run.sh <请求.toml>`。模型信号生成、完整性校验和验证集指标均在包内执行。

数据库导出与底物适配使用 `python -m tricycle_reaction_db.pools export|adapt`，离线分布统计使用 `analysis.sampling.reaction_collection_distribution`。

运行实现位于 [replay/](src/tricycle_sampling/replay/)，不依赖仓库的 `tools/` 或 `tricycle-exploration`。基础采样 API 只依赖 core；回放控制器使用 `tricycle-sampling[replay]` 的可选依赖。工作区可用以下命令：

```bash
uv run --package tricycle-sampling --extra replay python -m tricycle_sampling.replay.campaign --help
uv run --package tricycle-sampling --extra replay python -m tricycle_sampling.replay.suite --help
```

| 职责 | 包内入口 |
| --- | --- |
| 单实验、多 seed 逐轮监督 | `tricycle_sampling.replay.campaign` |
| 多方法统一套件 | `tricycle_sampling.replay.suite` |
| 远程单轮训练/推理 | `tricycle_sampling.replay.worker` |
| 远程整条基线轨迹 | `tricycle_sampling.trajectory_worker` |
| 结构信号与 RDKit 分析 | `tricycle_sampling.structure_analysis` |
| 状态、审计、故障恢复 | `tricycle_sampling.replay.report`、`audit`、`platform_audit`、`watch`、`recover_review`、`recover_retain` |

`campaign` 直接调用包内 `replay.selection.select_round`，校验模型证据、执行确定性采样并原子保存本轮记录。远程输入由 SAR staging 交付当前包，启动器使用 `python -m`，不再复制零散 tools 脚本。离线图表入口在 `analysis.sampling`。

默认 prepare/status 只操作本地文件。真实监督需要 `--live`，提交还需 `--allow-submit`；已有锁、预算、重试和提交回执保护继续生效。新 run 绑定当前包内实现 hash；恢复时校验原绑定，代码改变后拒绝静默接续，不自动更新旧 run 的 hash，也不加载历史源码。

`active_learning submit` 的提交回执使用顶层 `run_id/status/external_id/request_sha256/job_group_id`。
恢复要求 `status=submitted`、非空字符串任务 ID、相同任务组和匹配的请求哈希；旧字段、嵌套 ID、
缺失字段或未知字段均报错，保留原回执且不重提对应任务。未知提交结果由 `submission-inflight.json` 阻止自动重提。

新比较采用五条文献基线，每条绑定一篇不同的主引用。五条基线与智能策略共享实验流程，变化限定在选样策略。文献证据见[方法与文章清单](../../docs/sampling-baselines.md)，实验操作见[统一采样实验](../../docs/mae-intelligent-sampling.md)。

## 比较集合与来源

`LITERATURE_BASELINE_STRATEGIES` 定义以下顺序。主引用是近五年文章的实际使用出处；实现记录了迁移到 TriCycle 的差异。

| 策略 ID | 主引用 | 当前选择规则 |
| --- | --- | --- |
| `predictive-uncertainty` | Zhong et al., *Nature Communications*, 2025，[DOI](https://doi.org/10.1038/s41467-025-59812-0) | 总体预测不确定性降序 |
| `latin-hypercube` | Slattery et al., *Science*, 2024，[DOI](https://doi.org/10.1126/science.adj1817) | 固定 LHS 设计点依次映射到最近未选候选 |
| `ucb` | Wang et al., *Nature*, 2024，[DOI](https://doi.org/10.1038/s41586-024-07021-y) | Gaussian 收益后验上分位数降序 |
| `distance-uncertainty-product` | Shrestha et al., *Nature Communications*, 2024，[DOI](https://doi.org/10.1038/s41467-024-49011-8) | 最近已标记参考距离 × 预测方差 |
| `uncertainty-kriging-believer` | Souza et al., *JACS*, 2025，[DOI](https://doi.org/10.1021/jacs.5c02218) | 最大标准差逐个选样，批内用预测临时标签更新后验 |

`random`、`epistemic-uncertainty`、`aleatoric-uncertainty` 是独立采样策略，未纳入上述文献策略比较集合。共享随机初始集仍用于实验初始化。

来源与方法协议的唯一代码定义为 [protocols.py](src/tricycle_sampling/protocols.py)。五项文献协议版本均为 `1.0.0`；算法实现版本单独记录，当前 predictive 为 `1.1.0`，新增四项为 `1.0.0`。文献协议版本、策略实现版本和实验 schema 具有不同用途。

## 统一实验接口

[experiment.py](src/tricycle_sampling/experiment.py) 提供协议 `tricycle.matched-sampling/v1`：

```python
experiment_sampling_request(
    strategy, candidates, *, reference_features, budget, seed, design_size, offset
)  # 返回 SamplingRequest

select_experiment_candidates(
    strategy, candidates, *, reference_features, budget, seed, design_size, offset
)  # 返回 SamplingResult
```

两者仅接受上述五个规范策略 ID。逐轮回放的 [baselines.py](src/tricycle_sampling/replay/baselines.py) 与整条轨迹 [trajectory_worker.py](src/tricycle_sampling/trajectory_worker.py) 均调用此接口。统一套件的 intelligent 智能策略沿用采集表达式接口，共享同一主模型、候选空间、初始化、训练和评价流程。它使用版本化的覆盖/成本初始化提案、已揭示模型反馈和一次批次复核；参照由 [cost_guidance.py](src/tricycle_sampling/replay/cost_guidance.py) 生成，最终排序由智能体公式决定。

### 共同输入

每个 `Candidate.payload` 提供以下字段；adapter 使用白名单构造请求，不复制目标标签、未采成功掩码、未来成本或评价指标。

| 字段 | 共同定义 |
| --- | --- |
| `design_features` | 六维反应物结构计数经 `count/(1+count)` 变换后的向量 |
| `product_count` | 当前冻结先验结构中该底物对的路线数，正整数 |
| `prediction_mean` | 同一组先验路线的 RxnResid 预测能量均值 |
| `predictive_variance` | 同一组路线的总体预测方差之和 |
| `epistemic_variance` | 同一组路线的认知方差之和 |

六维顺序固定为重原子数、杂原子数、环数、芳香原子数、可旋转键数、氢键受体数。`reactant_structure_features()` 是显式的本地 RDKit adapter；一般数值采样接口不要求解析结构。

`reference_features` 为已经揭示有效训练标签的底物对结构表，必须非空、与当前候选 ID 不相交，并使用相同六维变换。所有五项都验证共同输入，即使 LHS 的选择不使用模型预测。模型覆盖缺失时应补全共同推断，不能静默退回随机选择。

### RxnResid 方差如何用于 Bayes-UCB

设某底物对有 `n` 条先验路线，路线预测为 `mu_j`，认知方差为 `v_epi_j`：

```text
mu_pair  = sum(mu_j) / n
v_reward = sum(v_epi_j) / n²
s_reward = sqrt(sum(v_epi_j)) / n
score    = -mu_pair + Phi_inverse(0.95) * s_reward
```

统一协议将 RxnResid 的认知方差作为期望能量的不确定性，在路线独立假设下构造 Gaussian 近似；`direction="minimize"` 表示偏好较低预测能量。总体预测方差包含 aleatoric 噪声，未自动替代收益后验方差。路线相关性未在此近似中估计；这也不是 Wang et al. 条件通用性任务的原始 bandit 后验。

Predictive 和 A Score 使用路线方差之和，UCB 使用路线均值的认知方差；这些是预先固定的策略定义。UCB 的低能量优化偏好与整体 MAE 目标的差别需在结果中说明。

### KB 的统一适配

统一入口注入 `StructuralKrigingBelieverFactory`，在同一 RxnResid 边际均值与 `s_reward` 上构造：

```text
K(i,j) = s_i * s_j * exp(-||features_i - features_j||² / (2 * ell²))
ell = 1
```

结构 RBF 提供显式的相关性假设，RxnResid 自身未提供这个联合协方差。每选一个候选，用其当前预测均值作为临时标签，条件化后再计算下一项标准差。adapter 使用逐步条件化，避免构造完整候选池协方差矩阵；不增加主 SAR 训练任务，临时标签不进入正式训练集或数据库。

Souza et al. 使用 RF 树标准差和批内临时重拟合。本实现保留顺序 KB 的选择机制，采用 pair 级 Gaussian 适配，来源记录中明确这一差别。

### LHS 的跨轮设计

统一协议固定 `[0,1]` 六维边界、采集 seed 和初始批次之后的设计总规模。`offset` 是已消耗设计点数。每轮重新生成同一个设计序列，从 offset 继续映射，并排除已有选择。离散候选映射及任意批次前缀均不保证严格的 Latin 分层性质。

共同条件、轨迹预算、评价节点和恢复约束由[实验控制器](../../docs/mae-intelligent-sampling.md)管理。`sampling_experiment.shared_inputs_sha256` 绑定当前候选、预测和参考集合；同一快照切换策略时应一致。不同轨迹采样历史分化后，该 hash 可以不同。

Replay 的确定性基线仅使用上述五个策略和当前 `sampling_experiment` schema；缺失或不支持的 schema 明确拒绝，不自动切换到旧选样算法。已有运行记录保留，新计算从显式配置准备新 run。下述通用候选 API 的其他策略仍可独立使用。

## 通用请求接口

插件 ID 为 `tricycle.sampling`，公开能力为 `candidate.validate`、`candidate.select`、`strategy.register`。Python 计划操作为 `SamplingPlugin.plan("select", request_mapping, context)`，执行为 `SamplingPlugin.select(request)`。

| `SamplingRequest` 字段 | 约束 |
| --- | --- |
| `candidates` | `Candidate` 序列；每项含唯一 `candidate_id` 和 `payload` |
| `budget` | 非负整数，不超过候选数 |
| `strategy` | 已注册名称；通用 API 默认值为 `random` |
| `seed` | 可选整数；LHS 和统一实验要求显式整数 |
| `metadata` | 上游上下文；新增基线配置位于 `metadata.baseline` |

`SamplingResult` 返回 `selected`、`remaining`、strategy、seed 和 metadata，提供 `selected_ids` 与 `to_dict()`。候选对象保留输入内容。

### 独立基线请求参数

下表用于通用 API。统一实验由 adapter 冻结参数，不应逐策略修改这些共同输入。

| 策略 | 候选字段 | `metadata.baseline` |
| --- | --- | --- |
| LHS | 稠密有限 `design_features` | 必填 `bounds`；`feature_field` 可改字段名；`design_size` 默认 budget，`offset` 默认 0 |
| Bayes-UCB | `reward_posterior_mean`、非负 `reward_posterior_std` | `quantile` 默认 0.95，须在 `(0.5,1)`；`direction` 默认 `maximize`，也接受 `minimize` |
| A Score | `design_features`、非负 `predictive_variance` | 必填 `reference_features`；`feature_field`、`variance_field` 可改字段名 |
| KB 默认 adapter | 联合后验按 ID 提供，见下文 | 必填 `posterior`；`observation_noise_variance` 默认 0 |

新增四项拒绝未知 baseline 参数。LHS 用固定边界归一化后的 Euclidean 距离匹配最近未选候选；A Score 用给定表征的 Euclidean 距离，不额外归一化，不按批内选择更新参考集合。UCB `maximize` 的分数为 `mu + Phi_inverse(q)*std`，`minimize` 为 `-mu + Phi_inverse(q)*std`。

Predictive 通用 API 接受方差 `predictive_variance`、标准差 `predictive_std` 或通用标量 `predictive_uncertainty`，按此顺序取值且不改变数值；epistemic/aleatoric 使用对应前缀。这三种字段表达不同的分数量纲，输入提供方必须明确所用量。显式分数优先于 `posterior_probabilities`。二分类概率输入在本地计算自然对数熵：

```text
predictive = H(mean(p_i))
aleatoric  = mean(H(p_i))
epistemic  = predictive - aleatoric
```

分类概率与回归方差是独立输入形式；RxnResid 回归实验使用上述方差输入。`UncertaintyScores.to_dict()` 输出三个 `*_uncertainty` 标量字段。旧 `_unc` 拼写、裸分量名、嵌套 `uncertainty` 表及 `posterior_probs` 均拒绝；密度加权策略只读取 `pair_density`。

### KB surrogate protocol

通用 `SamplingPlugin()` 默认使用 `GaussianKrigingBelieverFactory`。`baseline.posterior` 包含 `candidate_ids`、`means`、`covariance`，ID 与当前候选池完全一致且唯一，均值和协方差按此顺序排列。协方差必须有限、对称、半正定；允许数值容差和奇异矩阵。完整输入存储为 O(n²)，半正定校验为 O(n³)，适合小池和接口验证。

其他本地 surrogate 可通过 `SamplingPlugin(kriging_believer_factory=...)` 注入：

- `KrigingBelieverFactory` 提供稳定的 `adapter_id`、`version`、仅校验本地输入的 `validate(candidates, metadata)`，以及 `create(candidates, metadata, seed)`。
- `KrigingBelieverSession.predict(remaining)` 返回完整 ID 到 `SurrogatePrediction(mean, std)` 的映射；`believe(candidate, predicted_label)` 完成下一次打分前的条件化或临时重拟合。
- 每次选择创建独立会话；不得改写主 checkpoint、真实标签或上游输入。自定义 refit 的 seed、失败和开销必须显式记录。

`plan` 校验 KB 输入但不创建会话或拟合模型。统一实验使用结构 RBF adapter，独立 TOML 示例使用完整 Gaussian 协方差，两者应分别标明。

## 审计与复现

五项均以 candidate ID 升序打破同分。计划与结果自动生成 `metadata.sampling_protocol`，记录文章、DOI、证据位置、规则、适配、协议 ID/version/hash、实际策略版本、seed、budget 和决策 hash。结果另含 `metadata.sampling_details`，保存有效参数、分数或 KB 批内步骤；这两个字段由插件生成，请求不能覆盖它们。

`metadata.sampling_experiment` 由统一 adapter 生成，记录表征、路线聚合、Gaussian/RBF 假设与共同输入 hash。协议 hash 绑定协议内容，决策 hash 绑定决策记录；控制器源码、数据和 checkpoint 的文件 hash 由实验运行记录另外绑定。

内置 `random` 先按 ID 规范排序，再用局部 `random.Random(seed)` 抽取，不改变全局随机状态；`first-n` 保留输入顺序。

## 示例与执行

五项[完整 TOML 示例](../../configs/sampling-baselines/)演示独立 API，不构成统一实验配置：

```bash
uv run --package tricycle-agent tricycle-agent run \
  candidate.select configs/sampling-baselines/ucb.toml --run-id sampling-ucb-demo
```

选择本身不连接数据库、Bohr 或模型服务。组合应用将本地计划、结果和 trace 写入 run；未指定目录时使用本机时区的 `runs/YYYY-MM-DD/HH-MM-SS.ffffff±ZZZZ/<run-id>`。

新实验配置分别为[逐轮统一套件](../../configs/dpa-unified-sampling-suite.toml)和[整条 worker 轨迹](../../configs/rxnresid-matched-sampling.toml)。既有结果与 checkpoint 保留；重跑使用当前实现新建 run，不通过刷新旧 hash 迁移。

## 智能采集与自定义策略

`agent-authored-acquisition` 接收 `metadata.agent_acquisition`，用安全数值 DSL 执行已编写的表达式；调用模型和准备结构证据由上游负责。支持 `tricycle.agent-acquisition/v1` 和 MAE 协议的 `v2`，分别遵循各自输入约束。v1 要求公式组合新颖性、模型质量、采样分布与成本；v2 允许按证据自主选择信号，成本表达式保持可核查的正数估计，不强制成为评分惩罚。

证据必须绑定本地、已终态并已下载的计算报告及 artifact。DSL 只支持数值字段、context、estimated_cost、基础算术/比较/条件表达式，以及 `abs`、`exp`、`log`、`log1p`、`max`、`min`、`sqrt`；不执行任意 Python，不访问网络、随机数或系统状态。完整契约见 [acquisition.py](src/tricycle_sampling/acquisition.py) 和[智能采集 skill](../../.agents/skills/agent-authored-acquisition/SKILL.md)。

还注册 `pair-core-set`、`density-weighted-uncertainty` 和 `uncertainty-diversity` 等工程策略；其规则未自动绑定到上述五篇主引用。策略请求只接受注册的规范名称，不解析旧论文名称或其他拼写。

自定义策略实现 `SamplingStrategy` 的 `name` 与 `select(candidates, budget, seed)`，通过构造参数或 `register_strategy()` 注册；覆盖已有名称必须显式 `replace=True`。新增科学规则应有稳定版本、输入契约和来源记录。候选导出与外部写回通过独立 adapter 注入。

## 检查

```bash
uv run --frozen --all-packages pytest packages/tricycle-sampling/tests
```

候选重复、预算超界、未知策略、非有限分数或不完整后验会使请求失败。基础选择 API 没有远程副作用；包内回放控制器的任务恢复见[统一实验说明](../../docs/mae-intelligent-sampling.md)。

## 逐轮资产存储与传递

Replay 配置的 `asset_store` 指定项目级不可变资产库，缺省为工作目录的
`artifacts/task-assets`。固定 RxnResid runtime、先验结构、词表和私有数据仅保存唯一字节；
实验的 `asset-references.json` 记录引用，run 中不再保留 `assets/`、`private/` 输入目录或软链接。

每轮 CSV 与 worker 请求在临时目录生成后按 hash 入库；`input-assets.json` 引用完整交付内容，
包括该轮输入、固定 runtime、评价数据和本轨迹上一轮 checkpoint，
`input-assets-hash.json` 绑定该清单。run 中不再保留 `input/`，读取统一使用 `input_file`。
worker 请求仍绑定最终完整输入的逐文件 hash。Bohr adapter 提交时临时展开完整输入，
结束或失败后清理临时目录；当前 CLI 每个作业仍上传必要固定文件，不宣称跨作业远端缓存。

结果核验后，`result/` 和 `download/` 同样使用共享存储，保留原始下载包、日志、checkpoint、
原文件路径及逐文件校验记录；相同 checkpoint 在多个输出位置只占一份物理空间。
恢复和审计同时核对共享引用、完整输入及上一轮模型链。默认拒绝源码漂移，不刷新旧实验绑定。
仅在用户明确授权继续、逐项确认变更限于存储适配并验证输入字节等价后，可在实验目录
写入独立的 `storage-compatibility.json` 审计回执：schema 为
`tricycle.replay-storage-compatibility/v1`，记录原 `experiment_sha256`、`authorization`、
`reason`、`evidence_path`/`evidence_sha256`，以及 `changes` 中每个原控制器路径的
`before`/`after` 哈希。原配置和历史 manifest 保持不变；未列明的文件仍按原 hash 校验，
后续漂移再次拒绝。该回执不适用于算法、数据、训练或评价协议变更。

Worker 将训练临时文件放在 `work/round/`，下载选择器仅包含 `outputs/round/`。结果目录按明确清单
发布：主模型权重一份、辅助模型权重、预测、指标、生成的配置和必要训练日志/摘要。
上一轮上传权重、`refit-input.pt`、原始/预处理训练数据和主模型重复权重不进入结果目录。
无新标签而复用上一轮模型时，worker 在 `output_references` 中记录输入权重的 hash，不回传权重；
控制器核对同轨迹输入绑定后，在本地结果视图恢复该引用。失败时仍发布已有诊断日志。
