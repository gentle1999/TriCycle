---
name: reactionopt-enumerate-da
description: "Expand ene/diene substrate pairs into atom-mapped Diels–Alder reaction candidates and save a local CSV."
---

# 从 ene/diene 底物枚举 Diels–Alder 反应

## 适用场景

已有一批 ene/diene 底物组合，需要生成映射反应 SMILES 候选列表，供后续构象生成或计算任务准备使用。

## 输入与输出

- 输入：TOML 中一个或多个 `[[pairs]]`，每项提供 `ene_smiles` 和 `diene_smiles`，可选提供稳定的 `ene_id`、`diene_id`；
- 输出：运行目录下的 `mapped-reactions.csv`，每行是一条带全原子映射的反应候选；
- 默认保留已标注的底物 E/Z 相对构型，并枚举未指定的、非可翻转产品立体中心；
- 可翻转原子（`[#8,#16,#34;X3;v3;+1]`、`[#7,#15,#33;X3;v3;+0]`、`[#9,#17,#35,#53;X3;v3;+2]`）在产品中不指定立体标记，也不作为独立立体异构体枚举；
- 设置 `stereochemistry = "all"` 时，不按反应位点的底物 E/Z 关系过滤产品；
- 不连接 Bohr，不创建或提交计算任务。

## 执行

```bash
uv run --package tricycle-agent tricycle-agent run \
  reactionopt.enumerate-da configs/example-reactionopt-da-enumerate-request.toml \
  --run-id da-candidates
```

结果中 `metadata.candidate_count` 是候选数量，`metadata.enumeration_complete` 表示有无使用数量上限截断枚举。若要限制规模，可设置 `max_products_per_pair`（RDKit 反应匹配产物数）或 `max_isomers_per_product`；截断后 CSV 行上的 `enumeration_complete` 为 `false`。立体中心数目较多时，枚举量会快速增长。

## 输入示例

```toml
stereochemistry = "reactant-constrained"

[[pairs]]
ene_id = "ene-001"
ene_smiles = "C=C"
diene_id = "diene-001"
diene_smiles = "C=CC=C"
```

可选的 `output_csv` 指定 CSV 文件路径，`output_dir` 指定输出目录；未指定时写入本次 run 目录。相对路径按 `--workdir` 解析。

## 适用范围

内置反应模板面向中性 C/N ene 与中性 C/N 共轭二烯。枚举按子结构匹配生成所有映射连接方式，并枚举未指定的非可翻转产品立体化学；不会按 endo/exo 偏好、催化剂、溶剂或反应条件筛选候选。使用数量上限时，结果会明确标为不完整。
