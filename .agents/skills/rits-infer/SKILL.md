---
name: rits-infer
description: "Plan or explicitly submit a RitS inference job to Bohr from a TOML request."
---

# RitS 推理

`rits.infer` 规划原生 TS candidate 推理，命令由 `tricycle_rits.build_inference_command` 构造，默认入口为资产配置中的 `inference_entrypoint`。
需要经过过滤、聚类与能量筛选的完整 ReactionOpt profile 时使用 [reactionopt-profile](../reactionopt-profile/SKILL.md) 及 `build_ts_pipeline_command`；原始 candidate 输出不能冒充最终 profile。训练使用 `rits-finetune` 或 `rits-train`。

这里的 `request.toml` 是 TriCycle 的任务请求；RitS 自己的模型配置（例如 Hydra YAML）
属于随代码、checkpoint 和输入上传的任务输入 artifact，格式由 RitS 决定，不需要改写成 TriCycle TOML。
Bohr worker 执行请求的 `command`；若使用 `bash run.sh`，启动脚本必须已交付。初始化与依赖安装遵循所选镜像和任务配置，不在技能中维护另一套 pip 安装配方。

请求应包含 `model_name`、`image`、`code_artifact`、checkpoint artifact、`input_directory`、实际任务
`command` 和推理参数。`input_directory` 是 Bohr CLI 上传的本地任务目录，通常同时包含最新
RitS 配置指定版本的代码、checkpoint、原生 YAML 和输入数据。ReactionOpt profile
应调用 `scripts/run_ts_pipeline.py`，把单条 mapped reaction 写入 `--reactions` 输入文件，并保留
`selected_xyz`、`summary.json`、cluster summary 和 energy ranking。这里的
`scripts/run_ts_pipeline.py` 是 profile 使用的 fork-maintained pipeline，不属于官方 RitS 发布内容；底层的
`scripts/sample_transition_state.py` 只是 pipeline 的 candidate generation 阶段。
使用 dry-run 规划检查目标和输入，确认后才通过 `--live`
提交。状态查询、下载和失败恢复均依据 `run.toml.resume.next_action`，不要依赖聊天记忆。

默认模型来源与版本读取[模型资产配置](../../../packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml)的
`model.repository`、`model.revision`；权重按 `checkpoint_path` 缓存，
按 `task_checkpoint_path` 放入上传目录，并用 `checkpoint_sha256` 校验。
请求的 checkpoint artifact 指向任务内路径；技能不绑定具体 commit 或校验值。
自定义训练 checkpoint 使用其 artifact/运行记录中的实际绑定，不能套用默认预训练权重的校验值。

pipeline 默认使用 mapped stereo 过滤、重原子 complete-linkage Kabsch RMSD 聚类、medoid 初筛，
再对保留 cluster 成员执行配置指定的 g-xTB `xtb --gxtb --sp` 单点能量评估。g-xTB 可执行文件必须在
worker 环境中提供，或通过 pipeline 参数显式指定；不能用普通 GFN1/GFN2-xTB 代替。

## 不适用与失败恢复

没有 checkpoint、镜像或稳定 run 目录时先补齐 TOML，不要提交。Bohr 提交或查询失败时先 `inspect`，
确认没有已创建 job 后再 `resume --live`；已有 job 则转到 `bohr.job.status`。
