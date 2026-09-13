# tricycle-exploration

`tricycle-exploration` 为主动学习/反应空间探索提供一轮可复现的本地 provenance 记录。它不
提交 Bohr，也不自动启动 RitS；RitS 训练只由明确的 `training_policy` 决定。

## 一轮记录

`ExplorationPlugin.execute_round()` 对完整候选池执行已注册的 sampling strategy，并保存：

- 版本化的输入空间和空间 hash；
- 全量候选池、模型分数、模型快照、排除原因、排名和稳定 tie-break；
- `fixed`/`explicit` 范围策略及其输入指标；
- RitS 的 `train`、`finetune` 或 `skip` 决定、阈值和理由；
- 输入快照到下一轮快照的 `snapshot-transition.toml`。

训练被跳过时，下一轮快照严格等于输入快照。训练被安排但 checkpoint 尚未产出时，模块根据
输入快照、`round_decision_hash`、数据集 hash 和完整 `training_spec` 生成确定性的 planned
snapshot 与 replay key；训练产出后可传入 `next_model_snapshot` 绑定实际 checkpoint，而不改变
replay key。训练配方至少需要 `seed`、`dataset_manifest_hash`、`code_revision` 和
`container_digest`。QM 计算本身不在本包重做，候选输入和数据集 hash 已作为边界证据保存。

## 运行产物

```text
<run>/
├── trace/events.jsonl                  # 全运行 hash-chain 事件账本
├── request.toml                        # 原始请求
└── rounds/0001/
    ├── space.toml
    ├── candidate-pool.tsv
    ├── model-scores.tsv
    ├── selection-decisions.tsv
    ├── selected-candidates.tsv
    ├── range-decision.toml
    ├── training-decision.toml
    ├── snapshot-transition.toml
    ├── round.toml
    └── artifacts/manifest.toml
```

`resume = true` 只会复用 request hash 相同且产物完整的轮次，不覆盖已有文件。
CLI 运行状态中的 `resume.next_action` 保留为 `exploration.round`，用于安全复用/校验本轮；如果
gate 决定训练，`resume.follow_up_action` 会明确写成 `rits.train` 或 `rits.finetune`，实际执行
仍需提供独立的 RitS 请求。

## 示例

```bash
uv run --package tricycle-agent tricycle-agent run \
  exploration.round configs/example-exploration-round-request.toml \
  --run-id exploration-round-001
```

策略和训练门控的纯函数入口是 `decide_range()`、`decide_training()` 和
`resolve_snapshot_transition()`，便于在不写文件的测试中验证下一快照定义。
