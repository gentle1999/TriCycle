# 组件与能力契约

本文档描述Python 组件的实现接口。Codex 面向的公共调用单位是
`CapabilityManifest`/capability ID，架构与执行边界见 [`architecture.md`](architecture.md)。

## 公共生命周期

每个组件至少提供：

- `PluginManifest`：稳定的 `plugin_id`、版本、能力列表和依赖信息。
- 领域请求 dataclass：避免用未定义的字典在插件边界传播。
- `plan(...) -> TaskPlan`：纯规划阶段，生成任务参数、输入和目标操作。
- `execute(...)` 或领域专用副作用方法：通过注入的 adapter 执行；骨架阶段可以只返回 `planned`。
- 对应的能力 Skill：位于 `.agents/skills/<skill-name>/SKILL.md`，说明何时调用、输入、输出、失败处理和禁止事项。
- 如果能力参与长流程或主动学习，执行阶段还应通过 `ExecutionContext.trace_sink` 写入结构化事件；
  事件不能替代 artifact，完整输入和决定仍需落盘并带 hash。

## 能力契约

`tricycle-core` 的 `CapabilityManifest` 声明一个可独立调用的能力：

- `capability_id` 是稳定的 Codex 调用 ID；
- `component` 指向实际领域组件，不能反向让 core 依赖该组件；
- `input_kind`、`outputs` 描述 TOML 请求和 artifact 边界；
- `side_effect`、`requires_auth`、`supports_dry_run` 和 `resumable` 描述安全与恢复策略。

应用层的 `CapabilityCatalog` 将能力 ID 显式绑定到组件的 `operation`，
`CapabilityDispatcher` 负责注入 adapter 并执行。新的 Skill 不应直接 import 组件内部类。

## TaskPlan

`TaskPlan` 是跨插件的可审计中间表示，至少含有：

- `task_id`：在一次 run 内可追踪；
- `plugin_id` 与 `operation`；
- `inputs`：输入 artifact 或实体 ID；
- `parameters`：可 JSON 序列化的参数；
- `metadata`：来源、版本和幂等键等信息。

## 外部边界

- Bohr：实现 `BohrGateway`，至少覆盖 submit/status/download；批量生命周期可额外实现 job group、terminate 等操作；
- 反应数据库：写入单反应记录时实现 `ReactionRepository.upsert`；批次工作流实现
  `ReactionDatabaseAdapter`，负责按一个批次创建内部 project、提交计算日志、调用数据库
  rxn-SMARTS 筛选，以及导出 RitS/RxnResid 数据集；
- ReactionOpt：实现 `ReactionOptAdapter`，把中间配置转换成真实 ReactionOpt 调用；
- ReactionOpt profile：通过 `RitsTransitionStateSampler` 注入 reaction-level RitS fork pipeline，分成
  reactant 构象、product 构象和 RitS TS 三个独立分支，只在最终 profile 汇总时组合；TS 分支必须
  使用 `run_ts_pipeline.py` 的 candidate 生成、stereo 过滤、Kabsch RMSD 聚类和 g-xTB 筛选，不能把
  原始 `sample_transition_state.py` 输出直接当作最终 TS 集合。RitS 分支直接消费 mapped reaction
  connectivity，也不得回退到旧的 product-backward 初猜策略。输入
  reaction SMILES 必须为显式氢完整格式：每个氢是独立的带映射 `[H:...]` 原子，且所有原子
  均有唯一正映射，反应物/产物两侧映射集合和元素一致；
- 模型训练/微调/推理：RxnResid（`sar.*`）和 RitS 复用 `tricycle-bohr` 提供的 `BohrModelJobRequest`、
  `BohrModelPlugin` 和 `BohrJobExecutor`，分别生成 `train`、`finetune` 与 `infer` operation；
  领域组件负责自己的输入准备、源码/资产绑定和任务命令，复用公共提交边界。

protocol 不绑定具体传输实现。生产 adapter 包括 Bohr CLI、Reaction DB MCP/HTTP 和内网 SSH/Docker；领域请求与默认 dry-run 不创建这些客户端。

## 运行轨迹与探索轮次

`TraceStore` 将一次 run 的事件追加到 `trace/events.jsonl`，事件带连续序号、前一事件 hash 和
自身 hash；CLI 在 run/resume、plan、capability 完成或失败时记录生命周期事件。领域插件应在
事件中引用具体 artifact 的相对路径和 SHA-256，而不是只记录“已完成”。

主动学习的 `exploration.round` 必须把完整 candidate pool、模型分数和输入模型快照写入 round
目录，同时记录排除原因、稳定排序 tie-break、采样策略/版本/seed、范围策略和 RitS train/skip
证据。`snapshot-transition.toml` 以 `round_decision_hash`、数据集 hash、训练配方和输入快照
生成 replay key：skip 的后继快照必须是输入快照本身；训练尚未完成时使用确定性的 planned
snapshot，实际 checkpoint 返回后再绑定 `next_model_snapshot`。QM 计算不属于该重放函数，但其
候选输入和数据集 artifact hash 必须已存在。

`agent-authored-acquisition` 是无副作用的本地采样策略：智能体按轮次编写安全数值表达式，
`tricycle-sampling` 负责有限语法校验、确定性求值、成本排序和公式 hash。每次决策还必须引用
本地 `status.tsv` 与 `download-results.tsv` 的路径和 SHA-256；所有 worker 必须已进入终态，且
下载清单中的文件都存在。此检查将采样轮次与计算提交/运行分开，采样组件不能在同一计算任务中
提交候选或操作 Bohr 生命周期。各候选字段按所选采集协议校验；v1 与 MAE v2 的信号约束不同，见 [sampling 接口](../packages/tricycle-sampling/README.md#智能采集与自定义策略)。

## Bohr 工作流边界

`tricycle-bohr` 是单任务 Bohr 基础层：`BohrJobSpec`/`BohrGateway` 定义平台边界，
`BohrJobExecutor` 统一计划提交，`BohrModelJobRequest`/`BohrModelPlugin` 统一模型任务的
artifact、train/finetune/infer 和提交逻辑。RxnResid/RitS 提供领域请求、输入准备、资产校验和任务命令，
不重复实现 Bohr 提交。

`tricycle-bohrsubmit` 提供本地 staging、Gaussian/DPA-Berny 输入生成、TSV 生命周期和救援策略的类型化 Python API。云端 worker 仍生成小型 Bash 启动脚本，因为 Gaussian、DPA server 和用户提供的 ORCA 命令是镜像内的进程边界；`BohrCliGateway` 则集中处理 `bohr` CLI、重试和提交去重。

TriCycle 自己拥有的声明式请求和运行元数据使用 TOML。具体任务的配置文件属于输入
artifact，格式由任务自身决定，不要求组件为了适配 TriCycle 而改写，例如 RitS 的
Hydra YAML 配置可以原样随任务上传。环境变量保持独立：Bohr CLI 的认证凭据以及
worker 的 Bash 运行时变量由进程环境注入或通过 `.env`传递，不作为 TOML
配置内容保存。
