---
name: sar-finetune
description: Plan or explicitly submit an RxnResid checkpoint refit with compact records.
---

# RxnResid refit

遵循 [SAR 模块记录约定](../../../packages/tricycle-sar/README.md#实验记录与归档)，共同模型/数据/协议只定义一次，每轮只记录 seed/round、上一 checkpoint 引用、变化的输入和输出。不逐轮复制环境、协议、数据清单或平行摘要；执行证据保留在 `runs/`。

`sar.finetune` 对应 `train.py --refit --checkpoint <selection-checkpoint>`，使用原 checkpoint 的 config/fold/split/模型设置和新的 optimizer。允许的 refit 参数由 `tricycle_sar.build_finetune_command` 校验；不得同时传 `--config`、`--fold-index`、`--split-manifest`、`--seed` 或模型/encoder override。任务必须提供 checkpoint、代码 artifact、输入目录及实际命令；数据若需额外交付则声明 input artifact。

源码 revision 读取[模型资产配置](../../../packages/tricycle-sar/src/tricycle_sar/assets/rxnresid-model.toml)的 `model.revision`；执行环境与校验值读取当前后端配置，技能不绑定具体版本或 hash，不另写 pip/Torch/CUDA 安装配方。相同轨迹保持源码与 route 词表一致，checkpoint 不同版本拒绝混用。

```bash
uv run --package tricycle-agent tricycle-agent plan sar.finetune request.toml --run-id <id>
```

已有授权时经适配器 `run ... --live`；默认不联网、安装、训练或提交。已提交任务用状态与下载入口，已有 job ID 不盲目重提。完成后用原生 `predict.py` 产生不确定度输出；不能把输入准备或模型载入当作完成训练。

使用 `tricycle_sar` 包内环境与 runner；不保留 `tools/` 兼容入口，也不另建 protocols 源码树。worker 默认合并 predictions/metrics/status，checkpoint 按需显式保留；train/refit/infer 链条必须保留其后续使用的模型文件。

采样 refit 复用 `get_backend("rxnresid").refit_command`。累积数据变化由
`tricycle_sar.rxnresid.prepare_refit_checkpoint` 更新 checkpoint 副本的数据引用并重算目标归一化，
不修改原 checkpoint 权重。后续轮次须匹配相同 `sar_backend` 绑定。

当前新任务通过 `backends/catalog.json` 绑定共享环境引擎和执行入口；共同 core 文件按相同哈希随任务交付。只维护当前 catalog、源码与方法，不保留历史实现或兼容变体。结果中的方法 ID 仅记录计算来源；`results verify` 独立复算科学结果，`results prepare` 始终使用当前后端生成新输入，不执行训练或提交。

RxnResid 通用 train/refit/infer 与数据来源无关，使用请求提供的原生配置、CSV 和 checkpoint；反应数据库只是可选输入 adapter。当前采样与五折实验的列名、分组和路径聚合属于实验协议，不作为通用模型的数据约束。

`sar.train/infer/finetune` 使用共享命令 builder，镜像和机器类型匹配后端。本地输入目录中的源码与固定归档一致；规划记录输入哈希，提交再次核验。单任务执行复用 `single_job.py` 与 `sampling_runtime` 的共享初始化和源码，缺失输入的 dry-run 计划不能直接提交。累计标签训练按全 train 划分选择 from-scratch 模式，预测按逐路径身份校验覆盖。
