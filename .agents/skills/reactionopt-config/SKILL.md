---
name: reactionopt-config
description: "Generate a versioned, serializable ReactionOpt reaction configuration for downstream calculation."
---

# ReactionOpt 反应配置

## 适用场景

当采样器选出候选反应，需要在真正生成构象、反应路径或过渡态初猜前，先形成结构化 ReactionOpt 配置时使用。

## 输入

- 稳定的 `reaction_id`；
- 带 atom mapping 的 `reaction_smiles`；
- 反应模式/方法（骨架默认 `4+2`）；
- 构象、力场、路径生成等参数和可选输出目录。

## 工作流

1. 校验 reaction id、mapped reaction SMILES 和方法名。
2. 调用 `ReactionOptPlugin.generate` 得到内存配置。
3. 调用 `plan("generate", ...)` 保存版本化配置和运行上下文。
4. 由显式注入的 `ReactionOptAdapter` 负责调用现有 ReactionOpt 项目并导出文件。
5. 把配置版本和生成产物 artifact 传给 Bohr 或 reaction-db skill。

## 输出与约束

- 当前配置 schema 为 `tricycle.reactionopt/v1`；
- 默认只生成内存配置对象，不执行 RDKit/ASE/ReactionOpt 计算；用户请求和运行状态仍使用 TOML；
- 不在骨架层推断复杂反应中心、构象数量或 TS 搜索参数；
- 实际 ReactionOpt 版本和参数兼容性由 adapter 与后续插件规格负责。

实现入口：`packages/tricycle-reactionopt/src/tricycle_reactionopt/plugin.py`。
