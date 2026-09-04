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
- `BOHR_ACCESS_KEY` 等凭据通过环境变量或项目 `.env` 提供，不写入 TOML；shell 环境变量优先。

## 执行

先用默认 dry-run 检查：

```bash
uv run --package tricycle-agent tricycle-agent run \
  bohr.batch.submit request.toml --run-dir runs/<id> --run-id <id>
```

得到明确授权后再加 `--live`，必要时用 `--bohr-config configs/example-bohr.toml` 指定 adapter 策略。
提交结果和公开 job group ID 写入 `run.toml`，随后按 `resume.next_action` 进入状态查询。

## 失败与恢复

网关失败时先用 `inspect` 查看 `run.toml` 和 `submit-results.tsv`，确认凭据、项目和镜像后执行
`resume --live`。已成功记录的 worker 会按 worker 复用，不会重复提交；未完成 worker 才会重试。
