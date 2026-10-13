---
name: sar-infer
description: Plan or explicitly submit RxnResid inference with the current backend using shared inputs and compact records.
---

# SAR 推理

遵循 [SAR 模块记录约定](../../../packages/tricycle-sar/README.md#实验记录与归档)：源码、checkpoint、数据与预测协议引用一次，每任务只保存实际差异及输出。日志/回执/设备证据留在 `runs/`，不加入正式结果包或逐轮重复。

使用原生 `predict.py`，checkpoint 和推理数据以 artifact 传递；训练/refit 使用对应 skill。源码版本由模型资产固定，worker 复用已验证的镜像与安装入口，不能在任务中临时重写环境配方。命令由 `tricycle_sar.build_inference_command` 构造。

```bash
uv run --package tricycle-agent tricycle-agent plan sar.infer request.toml --run-id <id>
```

默认只规划。已有授权时通过 Bohr adapter 使用 `--live`；已有 job ID 先查询/下载，不盲目重提。

预测 CSV 包含 `aleatoric_variance`、`epistemic_variance`、`predictive_variance`、对应标准差和95%区间。校验 ID 覆盖后交给 sampling；统一 route 词表与 checkpoint 一致。`agent-authored-acquisition` 使用其已固化 SAR worker 生成 `candidate-model-signals.csv` 与模型质量材料，不在推理步骤选择或提交反应候选，继续遵循该 skill 的决策边界。

使用 `tricycle_sar` 包内环境与 runner；不保留 `tools/` 兼容入口，也不另建 protocols 源码树。worker 默认合并 predictions/metrics/status，checkpoint 按需显式保留；train/refit/infer 链条必须保留其后续使用的模型文件。

采样推理复用 `get_backend("rxnresid").inference_command`，通过 `source_directory` 和
`route_vocabulary` 使用同一原生入口与词表。独立 worker 需要交付 SAR 包及其依赖；
`tricycle_sar.sampling_runtime.stage(root)` 提供经校验的包、源码与原环境资产，默认不执行。

当前新任务通过 `backends/catalog.json` 绑定共享环境引擎和执行入口；共同 core 文件按相同哈希随任务交付。只维护当前 catalog、源码与方法，不保留历史实现或兼容变体。结果中的方法 ID 仅记录计算来源；`results verify` 独立复算科学结果，`results prepare` 始终使用当前后端生成新输入，不执行训练或提交。

RxnResid 通用 train/refit/infer 与数据来源无关，使用请求提供的原生配置、CSV 和 checkpoint；反应数据库只是可选输入 adapter。当前采样与五折实验的列名、分组和路径聚合属于实验协议，不作为通用模型的数据约束。

`sar.train/infer/finetune` 使用共享命令 builder，镜像和机器类型匹配后端。本地输入目录中的源码与固定归档一致；规划记录输入哈希，提交再次核验。单任务执行复用 `single_job.py` 与 `sampling_runtime` 的共享初始化和源码，缺失输入的 dry-run 计划不能直接提交。累计标签训练按全 train 划分选择 from-scratch 模式，预测按逐路径身份校验覆盖。
