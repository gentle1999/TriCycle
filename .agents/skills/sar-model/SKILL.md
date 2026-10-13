---
name: sar-model
description: Prepare unified SAR model benchmarks and validated backend tasks, including RxnResid train/refit/infer, with compact shared records.
---

# SAR 多模型任务

批量 worker 使用包内 `backends/workers/unified.sh` 及注册表绑定的 Python runner；不创建或引用模型专属、环境类别或 `tools/` 下的 shell 薄转发。公共执行与结果收集脚本由后端准备入口直接交付。

环境初始化直接执行 `backends/environments/engine.py`，由 catalog 交付为
`common/sar_model_envs/engine.py`；采样及本地环境入口使用同一引擎。
RxnResid 模型资产配置必须声明当前 `schema` 和 `[model]` 表；版本名与字段见包 README，顶层及表内的未知字段均拒绝。

`tricycle-sar` 管理统一的多模型后端、环境与训练/评估协议、任务准备及结果归档。使用 `get_backend(model)`、`tricycle_sar.preparation` 和 `python -m tricycle_sar` 选择模型并组织基准任务；方法来自包内 `backends/catalog.json`。“固化”指已通过 smoke 的环境初始化及训练/评估脚本，位于 SAR 包内并直接复用，不另建环境或升级依赖。

`tricycle_sar.SarPlugin`及 `sar.train`、`sar.finetune`、`sar.infer` 是其中的 RxnResid 任务接口。其源码版本与入口由 `assets/rxnresid-model.toml` 固定，训练、refit、推理分别使用 `train.py`、`train.py --refit --checkpoint ...`、`predict.py`。其他模型通过各自训练/评估 runner 预测；独立 checkpoint 推理和 refit 按后端实际支持的操作规划。

必须遵循 [SAR 模块实验记录约定](../../../packages/tricycle-sar/README.md#实验记录与归档)。共同数据、split、方法与参数写一次，任务只记录 fold/seed/round 等差异；`tricycle_sar.records.compact_tasks` 和 `tricycle_sar.submission`负责规范化。不复制逐折 JSON、环境安装脚本或平行的 manifest/scope 摘要。

1. 用 TOML 请求及 input artifacts 明确数据、checkpoint 和任务命令；源码版本/hash 必须一致。
2. 多模型任务通过后端或 benchmark 入口准备；RxnResid 原生任务通过 `sar.train/finetune/infer` 规划。默认无网络或提交。
3. 有既有授权时使用方法绑定的 Bohr 或内网执行器；RxnResid 能力提交使用注入 Bohr adapter 和显式 `--live`。已有 job ID 进入状态/下载。
4. 运行日志、checkpoint、回执与恢复信息留在 `runs/`；正式结果仅由模块的科学文件入口导出。五折预测合并为一份 OOF 表，拒绝逐折预测副本、`provenance/`、原始 worker 输入与逐任务日志。

RxnResid refit 从 checkpoint 恢复模型/config/fold/split，不传这些 override；升级源码时新建训练和采样轨迹，不混用旧 checkpoint。RxnResid 预测含原生 aleatoric/epistemic/predictive uncertainty，校验覆盖与固定 route 词表后供 sampling。

细分步骤见 [sar-train](../sar-train/SKILL.md)、[sar-finetune](../sar-finetune/SKILL.md)、[sar-infer](../sar-infer/SKILL.md) 和 [sar-benchmark](../sar-benchmark/SKILL.md)。

优先使用 `get_backend`、`tricycle_sar.preparation` 及 `python -m tricycle_sar`，不维护包外模型协议实现。默认 worker 输出合并预测、指标和任务状态；checkpoint 按需显式保留，refit/infer 需保留其依赖的模型文件。

采样任务复用 RxnResid backend 的 `training_command`、`refit_command`、`inference_command`，数据与
checkpoint 更新使用 `tricycle_sar.rxnresid`。用 `tricycle_sar.sampling_runtime.stage` 本地交付
经校验的源码归档和原环境入口，执行前 `verify`；不从历史 readiness 回退版本。

训练、采样和演示共用 SAR 当前后端。环境检查用 `python -m tricycle_sar.preparation.environment --group-config /path/to/group.toml` 显式绑定项目和任务组、生成可上传输入；已有数据重跑使用 `results prepare`，统一绑定当前方法并写入新目录；结果核验独立复算已记录的科学结果。公共指标、文件 I/O 和哈希来自 core，预测校验和底物对聚合来自 `tricycle_sar.predictions`。

RxnResid 通用 train/refit/infer 与数据来源无关，使用请求提供的原生配置、CSV 和 checkpoint；反应数据库只是可选输入 adapter。当前采样与五折实验的列名、分组和路径聚合属于实验协议，不作为通用模型的数据约束。

离线统计与科研绘图使用根目录 [`analysis/`](../../../analysis/README.md) 的共用实现和环境，入口为 `python -m analysis.sar render|distributions`；`tricycle_sar results` 保留 verify/prepare/assets。项目包不导入分析代码。

`sar.train/infer/finetune` 使用共享命令 builder，镜像和机器类型匹配后端。本地输入目录中的源码与固定归档一致；规划记录输入哈希，提交再次核验。单任务执行复用 `single_job.py` 与 `sampling_runtime` 的共享初始化和源码，缺失输入的 dry-run 计划不能直接提交。累计标签训练按全 train 划分选择 from-scratch 模式，预测按逐路径身份校验覆盖。
