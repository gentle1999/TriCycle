---
name: reactionopt-generate
description: "Generate a versioned ReactionOpt configuration from a TOML reaction request without external side effects."
---

# 生成 ReactionOpt 配置

## 适用场景

当已有反应 SMILES、反应 ID 和方法参数，需要生成可交给后续组件的版本化配置时使用。

## 输入与输出

- 输入：TOML，至少包含 `reaction_id` 和 `reaction_smiles`；
- 输出：`TaskResult.metadata.config`，包含 `schema_version`；
- 默认不读取凭据、不访问网络、不写远程系统。

## 执行

```bash
uv run --package tricycle-agent tricycle-agent run \
  reactionopt.generate request.toml --run-id <id>
```

需要先规划时使用 `tricycle-agent plan reactionopt.generate request.toml`。不要直接 import
组件内部类；如需导出文件，应在明确注入 `ReactionOptAdapter` 后调用 export 能力。

## 不适用

需要调用实际 ReactionOpt 工程或写入外部目标时使用 `reactionopt.export`；本能力不访问网络，也不
提交 Bohr 任务。

## 失败与恢复

TOML 或请求校验失败时修正请求并使用新的 `run-id` 重试；该无副作用能力通常不需要后续 resume。
