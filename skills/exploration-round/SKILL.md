---
name: exploration-round
description: "Record a strict, replayable exploration round and its next model snapshot transition."
---

# 探索轮次轨迹

当主动学习循环需要保存完整候选空间、采样选择、下一轮范围、RitS train/skip 决策及模型快照
迁移时使用 `exploration.round`。

```bash
uv run --package tricycle-agent tricycle-agent run \
  exploration.round configs/example-exploration-round-request.toml \
  --run-id exploration-round-001
```

请求必须包含完整 `candidates`、`budget`、策略和 `seed`，并用版本化 `space`、`range_policy`
和 `training_policy` 明确规则。Uncertainty 策略还必须声明 `model_snapshot`。训练被安排时，
`training_spec` 必须包含 `seed`、`dataset_manifest_hash`、`code_revision` 和 `container_digest`；
可选 `next_model_snapshot` 用来绑定已产出的 RitS checkpoint。

本能力只写本地 provenance，不提交 Bohr 或自动启动 RitS。运行目录会保存完整候选池、模型分数、
选择决定表、范围决定、训练决定、`snapshot-transition.toml`、artifact manifest 和
`trace/events.jsonl`。skip 时后继快照等于输入快照；train/finetune 时用输入快照和完整决策生成
稳定 replay key，checkpoint 未返回时记录 planned snapshot。

`resume = true` 只复用 request hash 一致且 artifact manifest 校验通过的轮次，不覆盖已有产物。
