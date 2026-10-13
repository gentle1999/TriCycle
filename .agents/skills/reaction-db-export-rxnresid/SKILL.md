---
name: reaction-db-export-rxnresid
description: "Export native reaction thermodynamics and adapt ene/diene identities across mapping conventions, or export the GT RxnResid contract."
---

# 导出 RxnResid mapped-reaction 数据集

## 原生反应数据集导出与底物适配

当任务是更新评测池/候选池并关联 ene/diene 时，优先使用数据库专用
`export_mapped_reaction_thermodynamics_csv` 工具，而不是本地重建标签或按 TS 输入来源展开数据。
项目现有入口为：

```bash
uv run --package tricycle-agent python -m tricycle_reaction_db.pools export \
  --config configs/database-sampling-pools.toml --live
```

省略 `--live` 只生成离线计划，不读取凭据或连接数据库。具体 CSV 下载由注入的
`ReactionThermodynamicsCsvAdapter.export_thermodynamics_csv(ReactionFilterRequest, destination)` 完成。
它将固定反应 SMARTS 作为数据库 `rxn_smarts` filter_expression 传递，按 offset 分页，
检查项目、行数、游标和字段一致性，保存完整原生 CSV 与 receipt。
采样数据集的配置必须设置 `[export_filters] has_activation_gibbs_free_energy = true`；
adapter 将此数据库原生参数传入每一页请求，receipt.arguments 保存筛选条件。
不能导出无标签行后再本地删除。返回值违反筛选条件时立即失败，不做本地筛除兜底。
`has_reaction_gibbs_free_energy` 也可显式传递；本次目标是活化自由能，不要求反应自由能同时非空。
已有原生导出可通过同一工具的 `adapt --config <配置> --root <导出目录> --output-root <新目录>` 离线复用适配；
该命令先检查项目限定的导出 receipt 和原始文件 SHA-256，不查询数据库。

数据库原生 CSV 每个热化学 profile 一行；保留全部理论层级、温度、能量和阶段耗时，
不自动选择 profiles[0]、minimum activation 或合并不同 profile。
这与下面原有的 GT RxnResid 六列导出契约不同，不能直接替换旧实验标签口径。

底物适配遵循 [反应数据集适配规范](../../../docs/reaction-dataset-adaptation.md)，执行前读取。
复用现有 `mapping-independent-stereo-graph-v2` 入口，不新增临时重组脚本。
先保留显式氢解析立体信息，再忽略 mapping 编号；以完整分子图的双向立体匹配判定身份。
连接关系字符串仅作检索，不能作为配对证明；保留 E/Z、手性、同位素、电荷和自由基。
旧 canonical-string 别名须回到同项目原始 mapped reactant/artifact 绑定重新验证，
禁止代理池使用 GT 别名或标签，也不能用 TS 输入编号展开原生数据行。
每个导出行补充 ene_id、diene_id、pair_id 和匹配状态，保留原始字段与行数。
无法唯一匹配的记录仍保留在主表，同时写入 unmatched.csv；不得猜测或因匹配失败删标签。
GT 写入 evaluation，代理池写入 candidates。修正后的原生耗时覆盖 mapping 可归属的全部
已记录候选计算（含未收敛尝试），由数据库按来源文件去重。total 是三个阶段的文件并集
成本，不用阶段相加重建；同一 mapping 的多个 profile 重复携带成本，不按行重复计费。
保留 seconds 与空值，不猜测 CPU/墙钟口径；核时换算和跨 mapping 共享资源计费须有明确
来源证明，具体规则见适配规范。
策略版本、RDKit 版本、原始行 hash 与匹配证据随 receipt 保存。
输入组合覆盖、数据库导出完整性和适配匹配覆盖分别报告；mapping 或匹配组合数变化
先按结构等价性核查，不能据此认定数据不全。更新策略后使用新输出目录，不沿用旧覆盖结论。

## 固定采样空间

本项目当前空间固定由 `configs/frozen-sampling-space.toml` 引用。后续实验使用此 pin，
先运行 `python -m tricycle_sampling.active_learning verify-frozen-db --config configs/frozen-sampling-space.toml` 离线校验；
不通过 latest 目录或重新查询数据库选择数据版本。保留此前实验自己的快照。
新的 RxnResid 实验设置 `[dataset] frozen_space_config = "configs/frozen-sampling-space.toml"`；
loader 自动绑定四个 collection，validate/prepare 再次核验 hash，并记录空间版本。

用户要求固化新空间时，用 `python -m tricycle_sampling.active_learning freeze-db --root <原生导出目录>
--snapshot-id <版本ID> --pin-config <新配置路径>`；旧 pin 和输出目录不得覆盖。
该操作要求原生 receipt 证明已传入 `has_activation_gibbs_free_energy=true`，
检查 GT 完整底物笛卡尔积、标签有限性及原始字段保留；不在本地挑选或删行。
完整的本次有标签原生导出与适配证据存入 audit/source-export，保留适配记录和结构注册表；执行实现由当前包提供，
以固定 manifest SHA-256 与逐文件 hash 锁定版本。不会查询数据库、挑选最小/平均 profile 或重建标签。
变更活动数据集时生成新版本 pin；只有用户授权换版时才更新当前引用，旧 pin 和快照继续保留。
供采样使用的注册表只有 ID/SMILES；含历史 QM 字段的原始注册表仅留在 evaluation/provenance。
GT 标签和评测结果仍不进入采样决策，含失败尝试的核时账本由实验另行固定。

## 原有 GT RxnResid 导出契约

此专用接口目前只接受实现限定的 GT 项目，`project_id` 从[请求配置](../../../configs/example-reaction-db-rxnresid-export.toml)读取；其他项目使用上面的原生 CSV 导出接口，不将 GT ID 当作全局默认值。请求必须设置 `cleaned_only = true`，并按数据库的实际
`reaction_smarts` 筛选；不要使用 `reaction_class`：

```text
[#6,#7]=,:[#6,#7].[#6,#7]=,:[#6,#7]-,:[#6,#7]=,:[#6,#7]>>[#6,#7]1[#6,#7][#6,#7][#6,#7]=,:[#6,#7][#6,#7]1
```

## 数据库 MCP 查询

通过注入的 `McpReactionDatabaseAdapter` 执行 `reaction-db.export-rxnresid`。adapter 使用数据库 MCP
应用的 `compose_query`，不调用 REST CSV 路由：

1. 调用 `MappedReactionQueryService.list_mapped_reactions`，传入 `project_id` 和完整
   `reaction_smarts`。分页读取所有 `items`，根据返回的 `page` 信息继续请求，直到遍历完项目结果。
2. 对每个 mapped-reaction summary，以其 `id` 和同一个 `project_id` 调用
   `ReactionEnergyQueryService.get_mapped_reaction_thermodynamics`。
3. 使用数据库返回的 mapped reaction SMILES 和 materialized thermodynamics profile。能量和阶段耗时
   必须取自同一个 profile；不得从 calculation log 本地重算、拼接或猜测。

查询必须通过注入的 MCP adapter 使用项目限定的查询构造；不要在 skill 流程中临时拼接任意
GraphQL。每次调用都必须使用同一个 `project_id`。映射查询使用 `reaction_smarts` 参数，不使用
`reaction_class`，也不需要额外的 `filter_expression` 或 REST `has_*` 参数。当前 `compose_query`
复杂度上限为 250；若将多个 thermodynamics 查询合并为 GraphQL aliases，按本字段投影每批最多 24 条，
超出时拆分。数据库返回的
项目内 mapped-reaction 记录及 thermodynamics materialization 是清洗后数据的权威来源；没有
materialized profile 时不能回退到本地日志。

## 数据集字段和单位

CSV 最低字段契约为以下六列：

- `mapped_reaction_smiles`
- `reaction_free_energy`
- `activation_free_energy`
- `reactant_compute_seconds`
- `transition_state_compute_seconds`
- `product_compute_seconds`

自由能从 profile 的 `reaction.gibbs_free_energy_kcal_mol` 和
`activation.gibbs_free_energy_kcal_mol` 读取，单位为 `kcal/mol`。阶段耗时从同一 profile 的
`reactants_running_time_seconds`、`transition_state_running_time_seconds` 和
`products_running_time_seconds` 读取，单位为秒。若阶段耗时为 null，CSV 对应单元格留空；不得估算。
能量或 activation/reaction profile 缺失时，该 mapped reaction 不满足数据集契约；从 CSV 排除，并在
receipt 记录排除数量。不得填造数值。

当前 MCP adapter 使用数据库返回的第一个 materialized profile（`profiles[0]`）；一行中的能量和阶段耗时
必须全部取自该 profile，不得把不同 theory、温度或 profile 的字段混在一起。没有 profile、profile
格式无效时导出失败，不得改查本地文件。profile、reaction/activation 对象或任一必需自由能缺失时，
排除该条并记录原因和数量。必需 CSV 列仍须输出；源阶段耗时为 null 时留空，不得估算。

## 输出和 provenance

CSV 列映射如下：

| MCP 返回字段 | CSV 列 |
| --- | --- |
| `mapped_reaction_smiles` | `mapped_reaction_smiles` |
| `reaction.gibbs_free_energy_kcal_mol` | `reaction_free_energy` |
| `activation.gibbs_free_energy_kcal_mol` | `activation_free_energy` |
| `reactants_running_time_seconds` | `reactant_compute_seconds` |
| `transition_state_running_time_seconds` | `transition_state_compute_seconds` |
| `products_running_time_seconds` | `product_compute_seconds` |

成功 receipt 必须声明 `RXNRESID_EXPORT_API_CONTRACT`，并保留 `compose_query` provenance，至少包含
MCP app name、查询的 service/method、`project_id`、原样 `reaction_smarts`、mapped-reaction 分页数、
导出行数以及 `energy_unit = "kcal/mol"`、`timing_unit = "seconds"`、`cleaned_only = true`。
同时记录因缺少完整热化学值而排除的记录数。不得记录 bearer token。任一 MCP 查询失败、返回错误、
项目 ID 不一致或 receipt 缺少上述证明时，不得把导出标记为成功。

运行时从环境读取 `TRICYCLE_REACTION_DB_MCP_URL`、`TRICYCLE_REACTION_DB_MCP_TOKEN` 和可选的
`TRICYCLE_REACTION_DB_MCP_APP_NAME`；不得把凭据写进 TOML。请求示例见
`configs/example-reaction-db-rxnresid-export.toml`。live 执行入口：

```bash
uv run --package tricycle-agent tricycle-agent run \
  reaction-db.export-rxnresid \
  configs/example-reaction-db-rxnresid-export.toml \
  --run-id rxnresid-ground-truth-export --live
```

CSV、receipt 和 run 记录放在 `runs/YYYY-MM-DD/<本地时间戳>/record/`；若设置了
`TRICYCLE_RUN_ROOT`，将 `runs/` 替换为该根目录。
