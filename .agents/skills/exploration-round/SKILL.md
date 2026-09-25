---
name: exploration-round
description: "Record a strict, replayable exploration round, including candidate-space decisions and the RitS train-or-skip gate."
---

# Exploration round provenance

## 适用场景

当主动学习或反应空间探索需要记录一轮完整的候选池、模型分数、采样选择、下一轮空间决定和
RitS 训练决定时使用。

## 输入

- `exploration_id`、正整数 `round_index` 和可选 `parent_round_id`；
- 完整 `candidates`、`budget`、策略名和随机 `seed`；
- `space`，描述本轮搜索空间及其版本；
- uncertainty 策略必须提供 `model_snapshot.snapshot_id` 和 checkpoint hash；
- 使用 `agent-authored-acquisition` 时，先完成上一批 worker 状态检查并下载所有结果到本地，
  再按 [`agent-authored-acquisition` skill](../agent-authored-acquisition/SKILL.md) 生成请求；
- `range_policy`：`fixed` 保持当前范围，`explicit` 使用请求中的 `next_space`；
- `training_policy`：`never`、`always`、`every_n_rounds`、`new_labels`、
  `every_n_or_new_labels`、`dataset_changed` 或 `manual`。
- 若训练被安排，`training_spec` 必须包含 `seed`、`dataset_manifest_hash`、`code_revision` 和
  `container_digest` 等可复现训练配方字段；可选 `next_model_snapshot` 用于绑定已经产出的 RitS checkpoint。

## 执行

```bash
uv run --package tricycle-agent tricycle-agent run \
  exploration.round configs/example-exploration-round-request.toml \
  --run-id exploration-round-001
```

这项能力只产生本地 provenance artifact，不提交 Bohr，也不会自动启动 RitS 训练。

## 输出

运行目录的 `rounds/NNNN/` 中会保存：

- `space.toml`：空间定义及 hash；
- `candidate-pool.tsv`：完整候选池和排除原因；
- `model-scores.tsv`：分数来源、模型快照和 checkpoint hash；
- `acquisition-function.toml`：仅智能体采集策略生成，记录逐轮公式、上下文、成本统计和证据；
- `selection-decisions.tsv`：排名、稳定 tie-break、选中与否及理由；
- `range-decision.toml`：下一轮空间的策略、输入依据和结果；
- `training-decision.toml`：RitS train/finetune/skip 决定及理由；
- `snapshot-transition.toml`：本轮输入快照、下一轮快照、训练配方和稳定 replay key；skip 时下一轮
  快照明确等于输入快照，训练尚未产出 checkpoint 时记录确定性的 planned snapshot；
- `artifacts/manifest.toml`：上述文件的 SHA-256 清单。

总运行 trace 位于 `trace/events.jsonl`，并通过 hash 链记录各阶段事件。`training_action = skip`
也是一条明确的决定，不能用“没有训练 run”推断跳过原因。
训练决策和采样/范围决策共同形成 `round_decision_hash`，再与输入模型快照、数据集 hash 和
`training_spec` 形成下一快照的 replay key；因此不需要重新执行 QM，也能验证下一轮快照定义是否一致。
CLI 的 `resume.next_action` 保留为可复用本轮的 `exploration.round`；若训练应当发生，真正的
`rits.train`/`rits.finetune` 会记录在 `resume.follow_up_action`，需使用独立的 RitS 请求执行。

## 约束

- 不要把范围变化或训练 gate 写成未版本化的临时代码；策略 ID、版本、参数和输入 hash 必须写入请求。
- 完整候选池和模型分数是复现采样结果的必要输入，不能只保存 top-N。
- 智能体采集策略只能在已有计算任务完成且结果下载到本地后独立运行；本能力不会提交计算任务。
- `resume = true` 只复用请求 hash 一致的既有 round；不覆盖既有 artifact。
