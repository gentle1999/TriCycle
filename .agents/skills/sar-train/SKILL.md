---
name: sar-train
description: Plan or explicitly submit a SAR training job with current backends and compact experiment records.
---

# SAR 训练

记录、任务装配和归档统一遵循 [SAR 模块约定](../../../packages/tricycle-sar/README.md#实验记录与归档)；五折比较使用 [sar-benchmark](../sar-benchmark/SKILL.md)。公共数据/协议/参数定义一次，fold/seed/round 只写差异，不另建逐任务 manifest 或安装配方。

普通 RxnResid 训练使用原生 `train.py` 和 `--fold-index`；从 selection checkpoint 重拟合使用 [sar-finetune](../sar-finetune/SKILL.md)。源码 revision 读取[模型资产配置](../../../packages/tricycle-sar/src/tricycle_sar/assets/rxnresid-model.toml)的 `model.revision`；镜像、初始化入口和校验值由当前后端配置提供，复用已验证 worker。技能不绑定具体版本或 hash，不自行重装 Torch/CUDA 或改建虚拟环境。

TriCycle 的请求使用 TOML；模型原生配置和数据作为 artifact 输入。先本地规划：

```bash
uv run --package tricycle-agent tricycle-agent plan sar.train request.toml --run-id <id>
```

已有训练授权时通过注入的 Bohr adapter 执行 `run sar.train ... --live`。计划中的源码与不确定度定义只放在 job 参数中，不在 metadata 再复制。输入必须完整且版本/hash匹配；相同实验共享固定 split，数据更新新建 run，使用当前方法。

回执、日志、checkpoint 和恢复状态留在 `runs/`；提交结果通过模块的科学文件导出入口，不复制整个 worker 目录。已有 job ID 时查询/下载；失败重跑新建 run，不覆盖成功或运行中的任务。默认 dry-run 不读凭据、不联网、不训练、不提交。

使用 `tricycle_sar` 包内环境与 runner；不保留 `tools/` 兼容入口，也不另建 protocols 源码树。worker 默认合并 predictions/metrics/status，checkpoint 按需显式保留；train/refit/infer 链条必须保留其后续使用的模型文件。

当前新任务通过 `backends/catalog.json` 绑定共享环境引擎和执行入口；共同 core 文件按相同哈希随任务交付。只维护当前 catalog、源码与方法，不保留历史实现或兼容变体。结果中的方法 ID 仅记录计算来源；`results verify` 独立复算科学结果，`results prepare` 始终使用当前后端生成新输入，不执行训练或提交。

RxnResid 通用 train/refit/infer 与数据来源无关，使用请求提供的原生配置、CSV 和 checkpoint；反应数据库只是可选输入 adapter。当前采样与五折实验的列名、分组和路径聚合属于实验协议，不作为通用模型的数据约束。

`sar.train/infer/finetune` 使用共享命令 builder，镜像和机器类型匹配后端。本地输入目录中的源码与固定归档一致；规划记录输入哈希，提交再次核验。单任务执行复用 `single_job.py` 与 `sampling_runtime` 的共享初始化和源码，缺失输入的 dry-run 计划不能直接提交。累计标签训练按全 train 划分选择 from-scratch 模式，预测按逐路径身份校验覆盖。
