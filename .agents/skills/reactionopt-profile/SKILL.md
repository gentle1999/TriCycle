---
name: reactionopt-profile
description: "Build a mapped ReactionOpt profile from three independent reactant, product, and reaction-level RitS branches."
---

# Mapped ReactionOpt profile

## 适用场景

输入一个完整 atom-mapped reaction SMILES，工作流分成三个独立分支：分别生成前体构象集合、
后体构象集合，并让 RitS pipeline 直接从 mapped reactant/product connectivity 生成并筛选 TS 构象。三条
分支只在最终 profile 汇总时合并；也不调用 ReactionOpt 上游的 4+2 product-stretch 或
从后体反向推导策略。

## 输入与输出

- 输入：TOML，必填 `reaction_smiles`；可选 `reaction_id`、`[sampling]`、`[rits]` 和
  `output_dir`；
- `reaction_smiles` 必须显式写出完整氢：每个氢都是独立的、带唯一正映射的
  `[H:<map>]` 原子；重原子不能保留隐式氢或通过 `[CH3:...]` 这类原子属性存储氢；
  反应物与产物必须包含相同的完整映射原子集合和元素；
- `sampling` 分别作用于 reactant 和 product 两个构象分支；每一侧的多个组分可以在该分支
  内部合并为一个结构进行采样，但两侧不会共享嵌入坐标，也不要求构象数量相同；
- `[rits.pipeline]` 使用 RitS fork 中新增的 `scripts/run_ts_pipeline.py`，不是官方 RitS 发布入口；
  pipeline 内部调用 RitS 模型生成 candidate，随后执行 stereo 过滤、Kabsch RMSD 聚类和 g-xTB
  能量筛选；`code_artifact`/`input_directory` 必须指向包含该脚本的 fork checkout；
- `source_repository`/`source_revision` 默认记录官方 RitS 模型和 checkpoint 来源；使用 fork pipeline
  时还应记录 fork 的仓库地址和 commit，不能把官方模型 revision 当作 pipeline 脚本版本；
- pipeline 的 `device` 使用 `cuda:0` 或 `cpu` 等上游生成器支持的设备名，不使用 `auto`；
- RitS 分支直接消费 mapped reaction connectivity，不读取 reactant/product 分支的 3D
  结构；
- RitS pipeline 对整个 reaction 只调用一次，`n_samples` 控制原始 candidate 数量；最终 TS 集合由
  pipeline 的 stereo、聚类和 g-xTB 阶段决定；
- 输出是 `profile.json`、reactants/products 构象文件和 `transition_states` SDF/XYZ 文件，
  不自动生成 `pre/ts/post` 路径；三个结构分支共享整条 reaction 的全局 atom-map 顺序，
  该顺序也会写入 `profile.json`。

## 执行

先检查无副作用计划：

```bash
uv run --package tricycle-agent tricycle-agent plan \
  reactionopt.profile configs/example-reactionopt-profile-request.toml
```

真实构建需要在应用中显式注入实现 `RitsTransitionStateSampler` 的 pipeline adapter。仓库提供的
`LocalRitsPipelineSampler` 可以调用本地 RitS checkout；云端 adapter 可以使用同一套
`RitsInferenceConfig.build_pipeline_command()` 提交/下载 Bohr worker。组件不会读取凭据、访问网络
或自行选择 transport。
没有 adapter 时，`reactionopt.profile` 的 dry-run 仍可规划，但 live 执行会明确失败。

## 安全与恢复

默认 dry-run 不执行构象优化、不调用 RitS、不提交远程任务。实际运行产生的每个 TS 样本
都保留 RitS 样本序号和固定 source revision；失败后检查 run 状态和
已写出的 profile 目录，修复 adapter/输入后使用新的 run ID 重试。不要回退到旧的
product-backward 策略来补救。

实现入口：`packages/tricycle-reactionopt/src/tricycle_reactionopt/reaction_profile.py`。
