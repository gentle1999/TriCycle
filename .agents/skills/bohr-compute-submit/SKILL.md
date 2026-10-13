---
name: bohr-compute-submit
description: "Submit one planned Bohr computational chemistry job through the Bohr adapter after explicit approval."
---

# 提交单个 Bohr 计算

先用 `bohr.compute.plan` 检查镜像、命令、资源和输入 artifact。
DPA 必须使用 GPU 加速并申请 GPU 节点，纯 Gaussian 只允许 CPU 节点；不接受用
额外 CLI 参数覆盖这一选择。得到授权后：

```bash
uv run --package tricycle-agent tricycle-agent run \
  bohr.compute.submit request.toml --run-id <id> --live
```

`--live` 才会构造 Bohr CLI adapter 并读取 `BOHR_ACCESS_KEY`（或自动发现的项目 `.env`）。公开 job ID 写入 `run.toml`，后续按
`resume.next_action` 查询状态；不要把 token 写入请求、状态或日志。

## 输出与边界

输出是公开 job ID 和 `run.toml` 状态；本能力只负责提交，不负责轮询或下载。默认 dry-run 不读取
凭据、不访问网络，只有 `--live` 才产生远程写入。shell 环境变量优先于 `.env`。

## 失败与恢复

提交错误时先检查 `run.toml` 是否已有公开 job ID，再检查 Bohr 环境变量和 adapter 日志。
只有确认远程没有成功创建任务后才用 `resume --live` 重试；否则先用 `bohr.job.status` 查询，避免重复提交。
