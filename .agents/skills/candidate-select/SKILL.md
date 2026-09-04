---
name: candidate-select
description: "Select the next reaction candidates deterministically from a TOML sampling request."
---

# 选择反应候选

## 适用场景

当需要从候选集合中按策略选择下一批反应，并保留 seed 以便恢复或复现时使用。

## 输入与输出

- 输入：TOML，包含 `strategy`、`budget`、可选 `seed` 和 `candidates` 表数组；
- 输出：选中候选和剩余候选；
- 这是无外部副作用能力。

## 不适用

需要随机但不可复现的在线策略、远程候选数据库或候选写回时，不要把这些副作用塞入本能力；
先通过独立 adapter 准备输入。

## 执行

```bash
uv run --package tricycle-agent tricycle-agent run \
  candidate.select request.toml --run-id <id>
```

相同输入必须使用相同 seed。不要把候选结果写回请求文件；结果由运行状态和输出 artifact 记录。

## 失败与恢复

策略或预算校验失败时修正 TOML 后重新运行；选择结果本身不产生远程副作用，也不需要提交或恢复。
