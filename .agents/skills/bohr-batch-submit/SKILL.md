---
name: bohr-batch-submit
description: "Submit a prepared Bohr batch run through the configured Bohr adapter after explicit approval."
---

# 提交 Bohr 批量任务

## 适用场景

仅当已有准备完成的 run 目录，并且用户明确授权远程提交时使用。

## 前置检查

- `run.toml`、`batch.toml` 和 `submit-plan.tsv` 存在；
- 已检查非空 worker 和预期镜像、项目、机器；
- DPA 必须使用 GPU 节点，纯 Gaussian 必须使用 CPU 节点；提交前重新核对
  `batch.toml` 的 engine/machine_type，不使用额外 CLI 参数覆盖资源；
- 对引用固化 SAR 协议的 ReactionT5 批次，完整本地 assets 必须随 worker 上传，提交前校验权重及 tokenizer/config 的 hash；缺失则拒绝，不让远端回退 HF 下载。
- `BOHR_ACCESS_KEY` 等凭据通过环境变量或项目 `.env` 提供，不写入 TOML；shell 环境变量优先。

## 执行

先检查运行状态，确认 `resume.next_action` 为 `bohr.batch.submit`，再用 dry-run 检查该动作：

```bash
uv run --package tricycle-agent tricycle-agent inspect --run-dir /absolute/prepared-run
uv run --package tricycle-agent tricycle-agent resume --run-dir /absolute/prepared-run
```

在已有提交授权范围内，对同一个 `resume` 命令加 `--live`，必要时用 `--bohr-config configs/example-bohr.toml` 指定 adapter 策略。已有 `run.toml` 的目录不能再次用 `run` 创建运行；状态已转入查询或下载时，不把 `resume` 当作提交入口。
提交结果和公开 job group ID 写入 `run.toml`，随后按 `resume.next_action` 进入状态查询。

## 失败与恢复

网关失败时先用 `inspect` 查看 `run.toml` 和 `submit-results.tsv`，确认凭据、项目和镜像后执行
`resume --live`。已成功记录的 worker 会按 worker 复用，不会重复提交；未完成 worker 才会重试。

## 直连网络

Bohr CLI adapter 在每次调用的子进程环境中移除 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY
（含小写），并设置 NO_PROXY/no_proxy=*。任务 API 与签名文件上传下载均直连；
不修改父进程环境或 Codex 决策请求的代理。已运行的长驻监督进程需等当前操作完成后重载代码。
