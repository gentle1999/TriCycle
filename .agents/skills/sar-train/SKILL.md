---
name: sar-train
description: "Plan or explicitly submit a SAR training job to Bohr from a TOML request."
---

# SAR 训练

普通训练要求 `--fold-index`。从 selection checkpoint 继续训练使用独立的
`sar-finetune` skill；RxnResid 的该流程在上游称为 `--refit`。

这里的 `request.toml` 是 TriCycle 的任务请求；RxnResid 的模型配置和数据清单属于随任务
上传的输入 artifact，格式由 RxnResid 入口决定，不要求统一改成 TriCycle TOML。

请求使用 `model_name`、`image`、数据集 artifact 和训练参数。长期 SAR 实现使用已构建的
`registry.dp.tech/dptech/dp/native/prod-23278/dl:260828` Bohr worker runtime；任务 artifact
携带当前 RxnResid 代码、`pyproject.toml` 以及任务原生配置。worker 使用系统 Python 直接安装
项目，不创建虚拟环境。当前上游固定为 `https://github.com/gentle1999/RxnResid`，commit 为
`ac2a0c59b80d430564309c77d26f0a0d69139009`；官方训练入口是 `train.py`，配置为
`configs/rxnresid.yaml`。先执行：

```bash
uv run --package tricycle-agent tricycle-agent plan sar.train request.toml --run-id <id>
```

只有用户授权后才使用 `run sar.train ... --live`。真实提交经过 Bohr adapter，公开 job ID 写入 `run.toml`；
模型 checkpoint 和数据集位置作为 artifact 传递，不把凭据写入配置。

推荐将 `image` 设置为 `registry.dp.tech/dptech/dp/native/prod-23278/dl:260828`，并在
`parameters` 中保存 RxnResid commit、数据版本、fold 和训练超参数。Bohr command 应通过
`bash run.sh`，由该脚本按 `pyproject.toml` 安装当前任务代码后执行其真实训练入口；TriCycle
请求使用 TOML，任务原生配置作为输入 artifact 传递；脚本中的命令形式为
`python3.12 train.py --config configs/rxnresid.yaml --output ... --fold-index 0`。

## 不适用与失败恢复

dataset、镜像或训练参数不完整时先修正 TOML。网关异常后先检查运行状态和公开 job ID；确认未创建
远程任务才使用 `resume --live`，已有任务则进入状态查询。
