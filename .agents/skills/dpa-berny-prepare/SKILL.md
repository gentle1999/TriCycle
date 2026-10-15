---
name: dpa-berny-prepare
description: "Prepare local DPA-Berny or Gaussian worker inputs from a TOML request without remote submission."
---

# 准备 DPA-Berny/Gaussian 输入

请求使用 `xyz_patterns`、`workers`、`engine`，以及 DPA 的 `model_path` 或 Gaussian 的 `functional` / `basis`；输出目录使用 `out_dir`。未知字段与旧别名直接报错。

`engine="dpa"` 必须使用 GPU `machine_type`；`engine="gaussian"` 必须使用 CPU
`machine_type`，不能互换。检查显式配置或 `BOHR_MACHINE_TYPE` 的实际值，不依靠
额外 CLI 参数覆盖资源。DPA-Berny 的能量/梯度由 GPU 模型服务计算，即使 Gaussian
优化器使用 CPU，也必须申请 GPU 节点。

准备命令：

```bash
uv run --package tricycle-agent tricycle-agent run \
  dpa-berny.prepare request.toml --run-id <id> --run-dir 'runs/<date>/<timestamp>/<id>'
```

该能力只创建本地输入和 `batch.toml`，不会提交 Bohr。检查 `.gjf`、`tasks.tsv` 和 `submit-plan.tsv` 后，
再进入 `bohr.batch.submit`。

DPA smoke 验收同时检查 `results/dpa-device.json` 的 CUDA 证据、服务实际 GPU 使用、
能量/梯度请求和优化结果；CPU 上跑完不算通过。CUDA 预检失败需排查环境或节点，
不允许改为 CPU 重跑以通过测试。细节见 [包文档](../../../packages/tricycle-bohrsubmit/README.md)。

## 不适用与失败恢复

该能力不提交远程任务，也不修改已有 run。XYZ、引擎或模型参数校验失败时修正 TOML，
换用新的空 `run-dir` 重新准备；保留失败目录供排查。

新 worker 输入在临时目录准备并发布到共享资产库；run 中仅保存输入引用与运行记录，
通过 `batch.toml.input_assets` 和 worker 的 `input-assets.json` 校验。提交须经过 adapter 临时展开，
不能直接上传 run 中的 worker 记录目录。
