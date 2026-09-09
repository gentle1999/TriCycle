# 组件与兼容插件契约

本文档描述现有 Python 组件的兼容接口。Codex 面向的公共调用单位是
`CapabilityManifest`/capability ID，目标架构见 [`architecture-design.md`](architecture-design.md)。

## 公共生命周期

每个兼容组件至少提供：

- `PluginManifest`：稳定的 `plugin_id`、版本、能力列表和依赖信息。
- 领域请求 dataclass：避免用未定义的字典在插件边界传播。
- `plan(...) -> TaskPlan`：纯规划阶段，生成任务参数、输入和目标操作。
- `execute(...)` 或领域专用副作用方法：通过注入的 adapter 执行；骨架阶段可以只返回 `planned`。
- 对应的能力 Skill：位于 `.agents/skills/<skill-name>/SKILL.md`，说明何时调用、输入、输出、失败处理和禁止事项。

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
- 反应数据库：兼容单反应记录时实现 `ReactionRepository.upsert`；批次工作流实现
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
- 模型训练/微调/推理：RxnResid（兼容 `sar.*` 命名）和 RitS 复用 `tricycle-bohr` 提供的 `BohrModelJobRequest`、
  `BohrModelPlugin` 和 `BohrJobExecutor`，分别生成 `train`、`finetune` 与 `infer` operation；
  领域组件只保留自己的 manifest、请求别名和任务命令构造。

这些接口当前不绑定 HTTP、CLI 或具体数据库 ORM。拿到具体插件说明后，再在 adapter 中选择实现方式。

## BohrSubmit 迁移边界

`tricycle-bohr` 是单任务 Bohr 基础层：`BohrJobSpec`/`BohrGateway` 定义平台边界，
`BohrJobExecutor` 统一计划提交，`BohrModelJobRequest`/`BohrModelPlugin` 统一模型任务的
artifact、train/finetune/infer 和提交逻辑。RxnResid/RitS 只提供领域 manifest、请求别名和任务命令构造，
不重复实现 Bohr 提交。

`tricycle-bohrsubmit` 负责把旧 `TriCycle-BohrSubmit` 的本地 staging、Gaussian/DPA-Berny 输入生成、TSV 生命周期和救援策略转换成类型化 Python API。云端 worker 仍生成小型 Bash 启动脚本，因为 Gaussian、DPA server 和用户提供的 ORCA 命令是镜像内的进程边界；`BohrCliGateway` 则集中处理 `bohr` CLI、重试和提交去重。

TriCycle 自己拥有的声明式请求和运行元数据使用 TOML。具体任务的配置文件属于输入
artifact，格式由任务自身决定，不要求组件为了适配 TriCycle 而改写，例如 RitS 的
Hydra YAML 配置可以原样随任务上传。环境变量保持独立：Bohr CLI 的认证凭据以及
worker 的 Bash 运行时变量由进程环境注入或通过 `.env` 兼容桥接传递，不作为 TOML
配置内容保存。
