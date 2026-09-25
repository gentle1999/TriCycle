---
name: sar-infer
description: "Plan or explicitly submit a SAR inference job to Bohr from a TOML request."
---

# SAR 推理

推理入口只读取 checkpoint 并生成预测 CSV；从 checkpoint 训练/重拟合使用独立的
`sar-finetune` skill。

这里的 `request.toml` 是 TriCycle 的任务请求；RxnResid 的模型配置和输入清单属于随任务
上传的输入 artifact，格式由 RxnResid 入口决定，不要求统一改成 TriCycle TOML。

请求应包含 `model_name`、`image`、checkpoint artifact 和推理参数。长期 SAR 实现使用已构建的
`registry.dp.tech/dptech/dp/native/prod-23278/dl:260828` Bohr worker runtime；当前 RxnResid
代码和推理入口随任务 artifact 上传，worker 使用系统 Python 直接安装项目，不创建虚拟环境。
当前上游固定为 `https://github.com/gentle1999/RxnResid`，commit 为
`9deebde39b38df1085dbac3a379c2a59edd7c97d`；该版本启用原生 evidential 不确定度推理。官方推理入口是 `predict.py`，配置为
`configs/rxnresid.yaml`。可使用 `tricycle_sar.build_inference_command` 生成命令。
默认只规划，不访问 Bohr：

```bash
uv run --package tricycle-agent tricycle-agent plan sar.infer request.toml --run-id <id>
```

真实执行需显式使用 `--live`，并通过环境变量或项目 `.env` 提供 Bohr 认证。完成后用 `bohr.job.status` 和下载能力继续。

推理命令通过输入目录中的 `bash run.sh` 安装当前任务代码后执行，脚本中的实际命令形式为
`python3.12 predict.py --checkpoint ... --data ... --output ...`。TriCycle
请求使用 TOML，任务原生配置作为输入 artifact 传递；不要把代码版本或任务配置假设写死在镜像中。
新版预测 CSV 会生成 `aleatoric_variance`、`epistemic_variance`、`predictive_variance`、
对应 `*_std` 以及 `lower_95`/`upper_95`。将预测行放入 candidate payload 后，
`epistemic-uncertainty` 策略会优先选择 `epistemic_variance` 最大的候选。

`agent-authored-acquisition` 模式可将 SAR worker 命令设为
`bash agent_acquisition_worker.sh agent-acquisition-worker.toml`，调用
`tools/agent_acquisition_worker.py`。该 worker 对剩余候选空间和有标签验证集分别运行 `predict.py`，
校验预测 ID 覆盖后生成 `candidate-model-signals.csv`、`model-metrics.json` 和 `worker-report.json`；
把 `outputs/agent-acquisition/` 声明为回传 artifact。它不选择或提交反应候选。下载后由本地流程将
`candidate-model-signals.csv` 中的预测字段并入候选 payload，再执行智能体采集函数。输入示例见
[`configs/example-agent-acquisition-worker.toml`](../../../configs/example-agent-acquisition-worker.toml)，
Bohr 请求模板见 [`configs/example-agent-acquisition-sar-infer-request.toml`](../../../configs/example-agent-acquisition-sar-infer-request.toml)，
worker 代码见 [`tools/agent_acquisition_worker.py`](../../../tools/agent_acquisition_worker.py)。

## 不适用与失败恢复

没有 checkpoint 或稳定输入 artifact 时先修正 TOML 并重新规划。提交失败时检查 `run.toml` 和 Bohr
状态；无公开 job ID 才可在确认后恢复，有 job ID 则先查询，不要盲目重提。
