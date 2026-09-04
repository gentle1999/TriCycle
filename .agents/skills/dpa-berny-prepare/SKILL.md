---
name: dpa-berny-prepare
description: "Prepare local DPA-Berny or Gaussian worker inputs from a TOML request without remote submission."
---

# 准备 DPA-Berny/Gaussian 输入

请求使用 `xyz`、`workers`、`engine` 以及 DPA 模型或 Gaussian 的 functional/basis。运行：

```bash
uv run --package tricycle-agent tricycle-agent run \
  dpa-berny.prepare request.toml --run-id <id> --run-dir runs/<id>
```

该能力只创建本地输入和 `batch.toml`，不会提交 Bohr。检查 `.gjf`、`tasks.tsv` 和 `submit-plan.tsv` 后，
再进入 `bohr.batch.submit`。

## 不适用与失败恢复

该能力不提交远程任务，也不修改已有 run。XYZ、引擎或模型参数校验失败时修正 TOML，
换用新的空 `run-dir` 重新准备；保留失败目录供排查。
