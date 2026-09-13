# 变更日志

## 2026-09-13

### 新增

- 增加基于 JSONL、哈希链和文件锁的运行轨迹记录，支持对任意快照进行完整性校验。
- 增加可复现探索轮次模块，记录完整候选池、模型分数、采样排序与排除理由、下一轮空间范围决策、RitS train/skip 决策以及快照迁移。
- 为 train、finetune 和 skip 迁移生成稳定的 replay key，使相同快照配合相同轮次决策可以复现下一个快照（QM 计算按相同输入可重复处理）。
- 增加 CLI run/resume 的请求、计划、能力执行和 trace 验证记录，并支持探索轮次安全复用。
- 增加 predictive、epistemic、aleatoric uncertainty 采样策略及候选分数的确定性记录。

### 验证

- `make check`
- `uv run pytest`（92 passed）
- `uv lock --check`
- `ruff check .`
- `git diff --check`
