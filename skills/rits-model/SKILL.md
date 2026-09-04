---
name: rits-model
description: "Create RitS training, fine-tuning, or inference jobs and submit them to Bohr using the shared job contract."
---

# RitS 训练、微调与推理

## 适用场景

当需要训练、微调 RitS 模型或调用已有 RitS checkpoint 对反应候选进行推理时使用。

## 输入

- `model_name`、`image`、`code_artifact`、`input_directory` 和可选 Bohr 资源；
- 训练必须提供数据集 artifact；推理必须提供 checkpoint artifact；
- 模式为 `train`、`finetune` 或 `infer`；
- 必须提供实际任务 `command`；RitS 官方训练/推理入口分别是 `scripts/train.py` 和
  `scripts/sample_transition_state.py`，可使用 `tricycle_rits` 的命令构造器生成 worker command；
- TriCycle 请求使用 TOML；RitS 模型专属配置作为任务输入 artifact，格式由 RitS 决定；`parameters` 只记录版本、路径、hash 和编排元数据。
- 当前上游仓库为 `https://github.com/gentle1999/RitS`，默认代码提交为
  `6d8aa0704c21d2798326a7e070491abf438ca292`，默认权重为官方
  `rits.ckpt`；组件级清单见 `packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml`。
- 本地缓存路径为 `artifacts/rits/rits.ckpt`；上传到 RitS 任务目录后必须位于
  `data/rits.ckpt`。
- `finetune` 使用 `finetune_from=<checkpoint>`，只加载权重并创建新的 optimizer/scheduler；
  `resume=<checkpoint-or-directory>` 是保留 optimizer、scheduler、epoch 和 step 的断点续训，不能混用。

## 工作流

1. 确认数据版本、任务类型、切分方式和随机种子。
2. 调用 `RitsPlugin.plan("train", ...)`、`plan("finetune", ...)` 或 `plan("infer", ...)`。
3. 检查生成的 `rits.train`/`rits.finetune`/`rits.infer` Bohr job 和 artifact 方向。
4. 只在显式授权后调用 `submit_to_bohr`，保存外部 job id。
5. 将模型产物和推理结果连同版本、配置、输入 hash 回写 reaction-db。

## 输出与约束

- 训练与推理均通过 `BohrJobSpec` 表达，便于与 Bohr 计算插件统一调度；
- `tricycle-bohr` 的 `BohrModelJobRequest`、`BohrModelPlugin` 和 `BohrJobExecutor` 负责共用的
  artifact、TaskPlan 和 Bohr 提交逻辑；RitS 组件只提供领域 manifest、请求别名和命令构造器；
- `TaskPlan.metadata.bohr_target` 必须为 `true`；
- 默认 dry-run，不直接调用模型服务或 Bohr；worker 使用已构建的深度学习镜像和系统 Python，
  不创建虚拟环境；
- RitS 的 YAML/Hydra 配置属于任务输入，不属于 TriCycle 元项目配置；
- RitS 微调仍使用上游 `finetune_from`；它要求 dataset 和 checkpoint artifact，不能用推理入口代替；
- 不猜测 RitS 的模型结构、输入格式和 checkpoint 约定，待插件规格补充。

实现入口：`packages/tricycle-rits/src/tricycle_rits/plugin.py`。
