---
name: sar-model
description: "Create RxnResid training, fine-tuning, or inference jobs and submit them to Bohr using the shared job contract."
---

# RxnResid 模型训练、微调与推理

## 适用场景

当需要基于已录入反应/分子数据训练 RxnResid 模型、使用 selection checkpoint 做 refit，或使用已训练 checkpoint 做推理时使用。

## 输入

- `model_name`、只包含公共依赖的 worker `image` 和可选 Bohr 资源；
- 上游代码固定为 `https://github.com/gentle1999/RxnResid`，默认 commit 为
  `9deebde39b38df1085dbac3a379c2a59edd7c97d`；该版本启用原生 evidential 不确定度推理；
- 训练时必须提供 `dataset` artifact；
- 推理时必须提供 `checkpoint` artifact，可再提供待预测输入；
- 必须提供实际任务 `command`，通常为 `bash run.sh`；`run.sh` 随 `input_directory` 上传，
  负责按 `pyproject.toml` 安装当前项目并执行上游入口；
- 模式为 `train`、`finetune` 或 `infer`；TriCycle 请求使用 TOML，模型配置和数据清单作为任务输入 artifact，格式由 RxnResid 入口决定；`parameters` 记录版本、路径、hash 和编排元数据。

## 工作流

1. 明确数据切分、标签版本、模型版本和随机种子，并写入 parameters。
2. 用 `SarPlugin.plan("train", ...)`、`plan("finetune", ...)` 或 `plan("infer", ...)` 生成计划。
3. 检查计划中的 `bohr_job.operation`、输入 artifact 和输出 artifact。
4. 在 Bohr 凭据、项目和镜像均已配置后，通过 `submit_to_bohr` 提交。
5. 将训练 checkpoint、预测结果、日志和 metrics 交给 reaction-db skill 录入 provenance。

## 输出与约束

- 训练 operation 为 `sar.train`，微调 operation 为 `sar.finetune`，推理 operation 为 `sar.infer`；
- `TaskPlan.metadata.bohr_target` 必须为 `true`；
- 默认 dry-run，不执行训练、不上传数据、不提交 Bohr；
- RxnResid 的任务配置和 split manifest 都属于输入 artifact，格式由任务入口决定；TriCycle 只在请求 TOML 和 provenance 中记录其路径、版本和 hash；
- 不把任务原生配置误当成 `run.toml` 或 adapter 配置。
- 官方训练/推理入口分别是 `train.py` 和 `predict.py`，配置文件为
  `configs/rxnresid.yaml`；可使用 `tricycle_sar` 的命令构造器生成 Bohr command。
- `predict.py` 输出 `aleatoric_variance`、`epistemic_variance`、`predictive_variance`、对应
  `*_std` 和 `lower_95`/`upper_95`；其中 `epistemic_variance` 可直接交给
  `tricycle-sampling` 做主动学习排序。
- `sar.finetune` 必须使用 `train.py --refit --checkpoint <selection-checkpoint>`；该模式从 checkpoint 读取 config/fold/split 和模型设置，在 train+valid 上以新 optimizer 重拟合，不能同时传 `--config`、`--fold-index` 或模型/encoder override。

实现入口：`packages/tricycle-sar/src/tricycle_sar/plugin.py`。
