---
name: rits-infer
description: "Plan or explicitly submit a RitS inference job to Bohr from a TOML request."
---

# RitS 推理

ReactionOpt profile 推理读取 checkpoint 并通过 RitS fork 中的 `scripts/run_ts_pipeline.py` 生成、过滤、聚类和筛选
transition-state 结构；从 checkpoint 开始训练使用独立的 `rits-finetune` skill。

这里的 `request.toml` 是 TriCycle 的任务请求；RitS 自己的模型配置（例如 Hydra YAML）
属于随代码、checkpoint 和输入上传的任务输入 artifact，格式由 RitS 决定，不需要改写成 TriCycle TOML。
Bohr worker 直接执行随 `input_directory` 上传的 bash entrypoint（例如 `bash run.sh`）。该脚本
使用系统 Python 执行 `python3.12 -m pip install --no-cache-dir --editable .`，再运行 RitS 入口，
不创建虚拟环境。

请求应包含 `model_name`、`image`、`code_artifact`、checkpoint artifact、`input_directory`、实际任务
`command` 和推理参数。`input_directory` 是 Bohr CLI 上传的本地任务目录，通常同时包含最新
RitS 代码、checkpoint、原生 YAML 和输入数据。command 通常为 `bash run.sh`；ReactionOpt profile
应调用 `scripts/run_ts_pipeline.py`，把单条 mapped reaction 写入 `--reactions` 输入文件，并保留
`selected_xyz`、`summary.json`、cluster summary 和 energy ranking。这里的
`scripts/run_ts_pipeline.py` 是 profile 使用的 fork-maintained pipeline，不属于官方 RitS 发布内容；底层的
`scripts/sample_transition_state.py` 只是 pipeline 的 candidate generation 阶段。
使用 dry-run 规划检查目标和输入，确认后才通过 `--live`
提交。状态查询、下载和失败恢复均依据 `run.toml.resume.next_action`，不要依赖聊天记忆。

当前默认模型版本记录在
`packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml`：
`https://github.com/gentle1999/RitS` 的
`6d8aa0704c21d2798326a7e070491abf438ca292` + `rits.ckpt`。请将权重缓存到
`artifacts/rits/rits.ckpt`，并在上传的 RitS 任务目录中放置为 `data/rits.ckpt`；请求的
checkpoint artifact 应指向后者。

pipeline 默认使用 mapped stereo 过滤、重原子 complete-linkage Kabsch RMSD 聚类、medoid 初筛，
再对保留 cluster 成员执行配置指定的 g-xTB `xtb --gxtb --sp` 单点能量评估。g-xTB 可执行文件必须在
worker 环境中提供，或通过 pipeline 参数显式指定；不能用普通 GFN1/GFN2-xTB 代替。

## 不适用与失败恢复

没有 checkpoint、镜像或稳定 run 目录时先补齐 TOML，不要提交。Bohr 提交或查询失败时先 `inspect`，
确认没有已创建 job 后再 `resume --live`；已有 job 则转到 `bohr.job.status`。
