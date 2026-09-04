---
name: bohr-compute-plan
description: "Build an auditable dry-run plan for one Bohr computational chemistry job from a TOML request."
---

# 规划单个 Bohr 计算

请求至少包含稳定的 `job_name`、`operation`、`image` 和 `command`，可选输入 artifact、项目和机器类型。

```bash
uv run --package tricycle-agent tricycle-agent plan \
  bohr.compute.plan request.toml --run-id <id>
```

该能力只有规划副作用，不读取认证、不访问网络，也不会提交任务。提交请改用 `bohr.compute.submit`。

## 失败与恢复

请求校验失败时只修改 TOML 请求并重新规划；不要把规划失败当作远程任务失败。

## 不适用

需要提交、查询或下载已有 job 时分别使用 `bohr.compute.submit`、`bohr.job.status` 或
`bohr.job.download`，不要让本 Skill 读取凭据。
