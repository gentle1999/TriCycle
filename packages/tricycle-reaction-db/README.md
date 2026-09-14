# tricycle-reaction-db

`tricycle-reaction-db` 是反应计算日志进入数据库以及从数据库导出训练数据的边界组件。
它负责请求校验、批次文件边界、数据库权威筛选条件、导出字段契约和 provenance receipt；
具体数据库 API、凭据和 schema 由注入的 adapter 实现。

## 能力

原生插件 ID 为 `tricycle.reaction-database`：

| 能力 | 作用 |
| --- | --- |
| `reaction.validate` | 校验兼容的单反应记录 |
| `reaction.ingest` | 通过 repository/adapter 录入单条反应记录 |
| `reaction.upsert` | 暴露兼容的 repository 写入边界 |
| `reaction.batch.validate` | 解析并审计一个计算日志批次 |
| `reaction.batch.ingest` | 一个新 project 导入一个批次 |
| `reaction.filter.cycloaddition` | 使用固定 cycloaddition rxn-SMARTS 筛选清洗数据 |
| `reaction.space.rits.export` | 导出 RitS 所需 mapped reaction + TS structure |
| `reaction.space.rxnresid.export` | 导出 RxnResid 所需能量和分阶段耗时 |
| `reaction.space.export` | 兼容旧名称的 RxnResid 导出入口 |

应用层的常用操作名是 `reaction-db.ingest`、`reaction-db.batch-import`、
`reaction-db.filter`、`reaction-db.export-rits` 和 `reaction-db.export-rxnresid`。

## 批次导入边界

`ReactionBatchImportRequest` 强制一次请求只创建一个内部 database project 并导入一个批次：

- project 使用小写 slug 和 name 描述，不能带已有 `project_id`；
- batch root 必须存在，默认只递归选择 `downloads/` 下的 `*.log`；
- symlink、越界文件、非普通文件和空批次会被拒绝；
- 计划保存 log 数量、总字节数和 manifest digest；
- Bohr 来源必须保留 job group ID、Bohr project ID、workflow 和初猜来源 provenance；
- 未显式提供 idempotency key 时，根据 project、batch 和 manifest digest 确定性生成。

这保证一次批量导入不会误把 Gaussian 输入、worker 输出或 sibling 目录文件上传到数据库。

## 数据库权威筛选

筛选固定使用数据库实际的 `reaction_smarts` predicate：

```text
[C,N]=[C,N].[C,N]=[C,N]-[C,N]=[C,N]>>[C,N]1[C,N][C,N][C,N]=[C,N][C,N]1
```

请求必须是 `reaction_filter="cycloaddition"`、`cleaned_only=true`，并使用上面的完整
`reaction_smarts`。`reaction_class` 不受支持；TriCycle 不在本地重新解释反应类别，也不从
日志自行推断 mapped reaction 关系。

## 两类标准导出

### RitS

`RitsDatasetExportRequest` 的最低字段契约为：

- `mapped_reaction_smiles`；
- 对应的 `ts_structure`，默认以 XYZ 物化；
- 数据库 project ID、固定 reaction SMARTS 和导出行数/provenance。

当前数据库没有同时返回这两类数据的单一 CSV 路由。adapter 必须按标准资源 API 读取：

- `GET /api/mapped-reactions`；
- mapped reaction detail；
- `GET /api/depictions/geometry/{geometry_id}.xyz`。

成功 receipt 必须记录 `RITS_EXPORT_API_CONTRACT` 以及这些标准请求。

### RxnResid

`RxnResidDatasetExportRequest` 通过标准 thermodynamics CSV endpoint 导出：

- `mapped_reaction_smiles`；
- `reaction_free_energy`、`activation_free_energy`；
- `reactant_compute_seconds`、`transition_state_compute_seconds`、`product_compute_seconds`。

请求固定使用 `POST /api/mapped-reactions/thermodynamics/export.csv`，时间单位为 seconds，
能量单位由请求声明。请求和 receipt 会保存 `RXNRESID_EXPORT_API_CONTRACT`，adapter 不能用
本地 query-and-compose 替代标准 API 合约。

## 架构和 adapter 边界

```mermaid
flowchart LR
    Q["TOML request"] --> V["typed request +\n固定 predicate 校验"]
    V --> P["TaskPlan\n或本地 batch manifest"]
    P --> A{"操作"}
    A -->|ingest| I["ReactionDatabaseAdapter"]
    A -->|filter| F["database-authoritative filter"]
    A -->|RitS export| R["mapped reactions + XYZ resources"]
    A -->|RxnResid export| X["thermodynamics CSV API"]
    I --> O["receipt + provenance"]
    F --> O
    R --> O
    X --> O
```

`ReactionDatabaseAdapter` 提供 live 的导入、筛选和两种导出方法；仓库内置的
`HttpReactionDatabaseAdapter` 对接数据库的 HTTP API，并使用流式 multipart 上传计算日志。
`ReactionRepository` 保留单条记录兼容边界。dry-run 只解析请求、发现文件、生成计划和 receipt
形状，不写数据库、不发 HTTP、不创建远程 project。测试可使用 `InMemoryReactionRepository`
或内存 adapter。HTTP adapter 的 URL、token、organization ID 和超时策略只从 live 环境变量
读取，不能保存进请求 TOML。

## CLI 示例

批次导入请求见 [`configs/example-reaction-db-batch-import.toml`](../../configs/example-reaction-db-batch-import.toml)，
筛选/导出示例见：

- [`configs/example-reaction-db-filter.toml`](../../configs/example-reaction-db-filter.toml)；
- [`configs/example-reaction-db-rits-export.toml`](../../configs/example-reaction-db-rits-export.toml)；
- [`configs/example-reaction-db-rxnresid-export.toml`](../../configs/example-reaction-db-rxnresid-export.toml)。

例如只规划 RitS 导出：

```bash
uv run --package tricycle-agent tricycle-agent plan \
  reaction-db.export-rits \
  configs/example-reaction-db-rits-export.toml --run-id db-rits-export-demo
```

真实导入或导出需要应用注入 adapter 并显式进入 live 路径；请求文件不应保存数据库凭据。

## 结果和验证

成功 receipt 至少记录 project ID、筛选器、行数、目标文件、API contract 和实际请求形状。
导出成功前会校验 receipt，错误 endpoint、`reaction_class` 或缺少标准 API provenance 都会被
拒绝。

```bash
uv run pytest packages/tricycle-reaction-db/tests tests/test_framework.py
make check
```

更完整的对象层次、查询边界和 skill 入口见 [`docs/reaction-db.md`](../../docs/reaction-db.md)。
