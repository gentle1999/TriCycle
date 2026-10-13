---
name: rits-model
description: "Create RitS training, fine-tuning, or inference jobs and submit them to Bohr using the shared job contract."
---

# RitS 训练、微调与推理

## 适用场景

当需要训练、微调 RitS 模型或调用已有 RitS checkpoint 对反应候选进行推理时使用。

## 输入

模型资产配置必须声明当前 `schema` 和 `[model]` 表，只接受模型声明的字段；格式与字段见包 README，平铺格式和未知字段均报错。

- `model_name`、`image`、`code_artifact`、`input_directory` 和可选 Bohr 资源；
- 训练必须提供数据集 artifact；推理必须提供 checkpoint artifact；
- 模式为 `train`、`finetune` 或 `infer`；
- 必须提供实际任务 `command`；RitS 官方训练入口是 `scripts/train.py`。ReactionOpt profile 使用
  RitS fork 中新增的 `scripts/run_ts_pipeline.py`（其内部调用 `scripts/sample_transition_state.py`
  生成 candidate）；原生推理使用 `build_inference_command`，profile pipeline 使用 `build_ts_pipeline_command`，不能互相替代；
- TriCycle 请求使用 TOML；RitS 模型专属配置作为任务输入 artifact，格式由 RitS 决定；`parameters` 只记录版本、路径、hash 和编排元数据。
- 默认模型来源、revision 与权重校验值读取[模型资产配置](../../../packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml)的
  `model.repository`、`model.revision`、`model.checkpoint_sha256`；不从技能中取固定值。
  profile 使用的 `run_ts_pipeline.py` 需要来自包含该脚本的 RitS fork。
- 本地缓存与上传目录内的权重路径分别使用配置的 `checkpoint_path` 和 `task_checkpoint_path`。
- `finetune` 使用 `finetune_from=<checkpoint>`，只加载权重并创建新的 optimizer/scheduler；
  `resume=<checkpoint-or-directory>` 是保留 optimizer、scheduler、epoch 和 step 的断点续训，不能混用。

## 工作流

1. 确认数据版本、任务类型、切分方式和随机种子。
2. 调用 `RitsPlugin.plan("train", ...)`、`plan("finetune", ...)` 或 `plan("infer", ...)`。
3. 检查生成的 `rits.train`/`rits.finetune`/`rits.infer` Bohr job 和 artifact 方向。
4. 只在显式授权后调用 `submit_to_bohr`，保存外部 job id。
5. 保存模型产物、推理结果及实际输入绑定；需要入库时另按数据库工作流处理，不假定训练完成就自动写数据库。

## 输出与约束

- 训练与推理均通过 `BohrJobSpec` 表达，便于与 Bohr 计算插件统一调度；
- `tricycle-bohr` 的 `BohrModelJobRequest`、`BohrModelPlugin` 和 `BohrJobExecutor` 负责共用的
  artifact、TaskPlan 和 Bohr 提交逻辑；RitS 组件只提供领域 manifest、领域请求类型和命令构造器；
- `TaskPlan.metadata.bohr_target` 必须为 `true`；
- 默认 dry-run，不直接调用模型服务或 Bohr；worker 使用已构建的深度学习镜像和系统 Python，
  不创建虚拟环境；
- RitS 的 YAML/Hydra 配置属于任务输入，不属于 TriCycle 元项目配置；
- RitS 微调仍使用上游 `finetune_from`；它要求 dataset 和 checkpoint artifact，不能用推理入口代替；
- 模型输入、命令与 checkpoint 契约读取当前资产配置、命令构造器和对应源码；缺少所选版本需要的输入时停止准备，不用占位规格替代。

实现入口：`packages/tricycle-rits/src/tricycle_rits/plugin.py`。
