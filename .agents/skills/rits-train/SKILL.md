---
name: rits-train
description: "Plan or explicitly submit a RitS training job to Bohr from a TOML request."
---

# RitS 训练

普通训练不提供 checkpoint override；从已有权重开始训练使用独立的
`rits-finetune` skill，断点续训使用 `resume=<path-or-run-directory>`。

这里的 `request.toml` 是 TriCycle 的任务请求；RitS 自己的模型配置（例如 Hydra YAML）
属于随代码和数据上传的任务输入 artifact，格式由 RitS 决定，不需要改写成 TriCycle TOML。
Bohr worker 直接执行随 `input_directory` 上传的 bash entrypoint（例如 `bash run.sh`）。该脚本
使用系统 Python 按 `pyproject.toml` 执行可编辑安装，再运行 RitS 入口，不创建虚拟环境。

请求使用 `model_name`、`image`、`code_artifact`、dataset artifact、`input_directory`、实际任务
`command` 和训练参数。`input_directory` 是 Bohr CLI 上传的本地任务目录，通常同时包含最新
RitS 代码、数据和任务原生配置；command 通常为 `bash run.sh`。
RitS 官方训练入口为 `scripts/train.py`，仓库提供的 `build_training_command` 可生成基础命令。
当前使用的官方代码提交和默认预训练模型记录在
`packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml`。若从预训练模型继续训练，
将 `artifacts/rits/rits.ckpt` 复制到任务目录的 `data/rits.ckpt`，并在任务原生参数中使用该路径。
当前上游为 `https://github.com/gentle1999/RitS`，commit 为
`6d8aa0704c21d2798326a7e070491abf438ca292`。
先规划并检查生成的 Bohr job：

```bash
uv run --package tricycle-agent tricycle-agent plan rits.train request.toml --run-id <id>
```

远程提交必须由用户授权并加 `--live`；凭据来自环境变量或自动发现的项目 `.env`，公开任务 ID 和后续动作写入 `run.toml`。

## 不适用与失败恢复

缺少 dataset、镜像或训练参数时只修改 TOML 并重新规划。提交异常时先检查运行状态和公开 job ID，
确认未提交后才恢复；已经提交则使用 `bohr.job.status`，不要重复创建训练任务。
