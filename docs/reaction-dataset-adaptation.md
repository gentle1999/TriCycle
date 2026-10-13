# 反应数据集适配规范

适用入口：`python -m tricycle_reaction_db.pools export / adapt`。
底物匹配策略版本为 `mapping-independent-stereo-graph-v2`。

## 数据来源与记录粒度

使用数据库专用 `export_mapped_reaction_thermodynamics_csv`，通过注入的 adapter 按项目和固定
rxn-SMARTS 分页下载。数据库负责清洗、反应关系与热化学；本地只做格式适配和底物身份关联。
保留原生 CSV、分页 receipt 与 SHA-256，不从计算日志重建能量，也不猜测数据库排除原因。
采样数据集显式使用数据库原生 `has_activation_gibbs_free_energy=true`，由数据库在导出时
排除无活化自由能标签的 profile。配置位于 `[export_filters]`，每一页 request 与 receipt.arguments
均记录相同条件。返回无标签行时失败，不在本地补做筛选；反应自由能非空筛选是独立可选条件。

每个原生热化学 profile 行保持独立。适配只追加字段并按组合排序；通过 `export_row_index`
回到固定原始导出行，逐字段检查原始 SMILES、理论层级、温度、能量与阶段耗时保持不变。
同一 mapped reaction 的多个 profile 不合并、不自动取最小值或平均值。
跨项目的 mapped reaction ID、mapping hash 和 RXN SMILES 字符串不能直接作为跨版本等价证明。

## 底物身份匹配

- RXN SMILES 中的 atom mapping 编号、原子遍历顺序和两种反应物排列不决定底物身份。
  数据库的新规范可能按反应物遍历与完整反应原子对应关系重新编号；原始映射字符串仍原样保存。
- 先以 `removeHs=False` 解析完整分子图，保留显式氢参与定义的 E/Z 与手性，之后清除 map 编号，
  再通过 RDKit 的氢移除操作压缩普通氢。同位素氢等不能当普通氢删除。
  禁止先用默认 SMILES 解析删除氢，再凭写出的 canonical SMILES 推断立体身份。
- 连接关系字符串仅作为候选检索键。最终必须比较等原子数、等键数的完整分子图，执行双向
  `useChirality` / `useEnhancedStereo` 匹配，并核查元素、同位素、电荷、自由基、氢数和芳香性。
  E/Z、对映体及“未指定立体信息”保持不同身份；不能去掉立体信息来凑足组合数。
- 本策略沿用声明的氧化物等价写法 `X(+)-O(-) ↔ X=O`。不执行泛化 Cleanup、互变异构合并、
  中和或额外立体重新分配；新增化学等价规则须版本化并验证其适用范围。
- 优先与底物注册表的原始结构匹配，两个 reactant 排列都要检查。只有无法形成直接匹配时，
  才使用同项目已验证的 reactant/artifact 绑定作为结构别名；别名不能覆盖已有的直接结构匹配。
  默认配置只使用注册表，不依赖历史运行目录的别名缓存。
- 旧 canonical-string 别名索引不能单独作为证据。重新读取其项目限定原始查询中的
  `participants.mapped_smiles` 与 reactant geometry 的底物文件绑定，记录证据文件 hash。
  禁止用 TS 输入编号、旧字符串近似、GT 标签或 GT 别名补代理池配对。

`pair_id` 表示此策略匹配出的 ene/diene 身份。原始计算输入组合是另一种 provenance，
不能借其关系复制原生数据行，也不能把它替代结构匹配结果。

## 结果状态与完整性

`matched` 必须有唯一组合及匹配方法；`ambiguous` 保存所有候选组合，不任取一个。
`unmatched`、`outside_registry`、非法 SMILES 或非双反应物记录保留在主表，并另列原因。
`reactant_connectivity_keys` 没有完整立体身份，禁止将该字段当成匹配证明。
每行记录匹配策略和方法，receipt 记录 RDKit 版本、来源 hash、别名绑定 hash 与各状态数量。

分别报告以下口径，不互相代替：

1. 原始计算输入覆盖（ene 与 diene 输入集合的笛卡尔积）。
2. 数据库正式导出完整性（项目、筛选、分页、记录数与原生文件 hash）。
3. 当前适配策略的匹配覆盖（全部行、有标签行、唯一组合及失败原因）。

匹配组合数少于输入组合数，或新旧 mapping/字符串不一致，只能触发适配核查；不能据此认定
数据库缺数据。先检验新旧表示的分子图等价性，再结合项目限定的数据库来源证据说明差异。
“每行已分配 ID”不等于“配对已验证”；旧版本的覆盖统计不自动继承为新版结论。

## 隔离与重放

GT 存入 `evaluation`，代理池存入 `candidates`；匹配只使用结构与身份字段，不使用能量或评测结果。
候选池的别名证据只来自自身项目。GT 标签与表现不能进入采样决策。
数据库阶段耗时保持原值与空值。原生四个 `*_running_time_seconds` 字段
统计当前 mapping 可归属的全部已记录候选计算，包括有来源记录的未收敛候选；端点按具体
参与物拓扑收集，TS 按 mapping 几何绑定和推断来源收集。数据库按 ArtifactFile 去重，
使用各文件最新来源 revision 的文件级用时；total 对三个阶段的文件并集再次去重，
不能用阶段值简单相加来重建 total。任何已归属文件的用时未知时，相关汇总保留空值。
同一 mapping 的不同 profile 重复携带同一成本，计费不能按 profile 行重复累加。
导出仍以 seconds 表示，单位换算与跨 mapping 共享资源计费另由固定的实验成本账本明确；
没有 CPU 时间或核心数的来源证明时，不将 seconds 字段直接命名为 core_hours。

已有正式导出可离线重放，策略更新使用新的输出目录并保留旧结果：

```bash
uv run --package tricycle-agent python -m tricycle_reaction_db.pools adapt \
  --config configs/database-sampling-pools.toml \
  --root /absolute/path/to/native-export-run \
  --output-root /absolute/path/to/new-adaptation-run
```

验证至少覆盖显式氢的环状烯烃、map 重编号、非恒等原子重排、reactant 顺序变化、对映体、
同位素及缺少立体指定的反例。生成测试 SMILES 时也要确认重写没有改变源分子图；不能把
RDKit 写出过程中改变的立体状态误称为等价输入。

## 固定采样空间

当前固定空间使用 `configs/frozen-sampling-space.toml`，它指向唯一的快照 manifest 并固定其
SHA-256。后续新实验先离线校验此 pin，再使用 manifest 中指定的四个 collection；不通过
latest 目录或数据库查询替换版本。历史实验仍保留自己的数据与结果。

```bash
uv run --package tricycle-agent python -m tricycle_sampling.active_learning verify-frozen-db \
  --config configs/frozen-sampling-space.toml
```

新的 RxnResid 实验配置设置 `[dataset] frozen_space_config = "configs/frozen-sampling-space.toml"`。
loader 从 pin 获取 GT 和三个候选 collection，并拒绝与 pin 不同的显式 collection；
`validate` / `prepare` 再次校验完整快照，将 snapshot ID 和 manifest hash 写入实验 provenance。
各项目的 resources 仍需配置其完整核时账本。

只有用户明确要求固定另一份空间时才生成新 pin：

```bash
uv run --package tricycle-agent python -m tricycle_sampling.active_learning freeze-db \
  --root /absolute/path/to/verified-export \
  --snapshot-id selected-space --pin-config /absolute/path/to/new-pin.toml
```

固化要求原生 receipt 证明已使用活化标签筛选；直接保存全部数据库筛选后行，本地不再筛选。
完整的有标签原生与适配证据留在 audit/source-export。
保留行的原始字段和 export_row_index 不变，不选择最小/平均标签或按共享 TS 去重；检查 GT 的有标签组合
与注册表的完整 ene×diene 集合相等。新快照保存数据、适配记录和逐文件哈希，不归档执行源码，且不覆盖既有 pin。
`substrate-registry.csv` 只含 ene/diene ID 与 SMILES；原始注册表中的历史 QM 字段仅归档在
`evaluation/provenance`，不能交给采样智能体。校验是控制器的离线审计，不是 acquisition 输入。
修正后的原生耗时已包含可归属的失败候选；实验成本账本另固定核时换算、跨 mapping
共享资源计费和 profile 重复成本的处理规则，不从原生表重新构造能量标签。
活动数据集内容修正采用新版本快照；当前配置的换版须有用户授权，原 pin 另存并可继续校验。
