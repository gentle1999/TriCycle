# 离线分析与绘图

`analysis/` 保存项目的离线结果分析和科研绘图代码，共用本目录的 `pyproject.toml` 与
`uv.lock`。模型任务、采样决策、环境初始化和科学结果校验由 `packages/` 提供；分析代码
读取这些接口和实验产物，项目包不反向依赖 `analysis`。

| 位置 | 内容 |
| --- | --- |
| `sar/figures.py` | 五折 MAE、OOF parity、绝对误差 CDF 与 GT 分布绘图 |
| `sar/distributions.py` | GT 总体、组内残差和组均值残差的描述统计 |
| `sar/distribution_figures.py` | GT 分布的投稿图布局、图注与来源记录 |
| `sampling/replay_sampling_panels.py` | 当前文献基线与智能采样的固定快照及绘图 |
| `sampling/threshold_cost.py` | 首次达到指定 GT MAE 的成本、配对比较与达标覆盖数 |
| `sampling/publication_curves.py` | 从已校验快照绘制三空间采样曲线、全量经验误差下限及达标成本 |
| `sampling/route_audit.py` | 核对已执行智能采样决策，重放排序并汇总分阶段结构、成本及路线变化 |
| `sampling/rxnresid_active_learning_analysis.py` | 已下载采样轨迹的成本校正、合并与绘图 |
| `sampling/reaction_collection_distribution.py` | 反应集合、路径覆盖与成本的离线分布统计 |

所有入口使用下文列出的具体分析模块，不保留 `tools/` 转发。在线探索的结构特征、
验证反馈、成本指标和运行摘要由 `tricycle-sampling` 提供；数据库底物身份适配由 `tricycle-reaction-db` 提供。

## 使用

在项目根目录执行；分析入口直接读取本地 workspace 的包接口，隔离环境只包含分析依赖：

```bash
uv run --project analysis --locked python -m analysis.sar render \
  --result-dir /path/to/result-package --out-dir /path/to/empty-figures
uv run --project analysis --locked python -m analysis.sar distributions \
  --result-dir /path/to/result-package --out-dir /path/to/empty-distributions
uv run --project analysis --locked python -m analysis.sar distributions \
  --result-dir /path/to/result-package --out-dir /path/to/empty-publication --style publication
```

SAR 分析先调用 `tricycle_sar.results.verify` 核对科学结果。输出写到结果包之外，拒绝覆盖
非空目录。五折图导出 PNG/PDF/SVG；分布投稿图导出 SVG/PDF/PNG/TIFF，并记录实际源码和
依赖锁的哈希。图表数值仍来自相同的冻结表和统计定义。

直接读取原生反应集合 CSV 的两面板分布图使用：

```bash
uv run --project analysis --locked python -m analysis.sar collection-distributions \
  --dataset /path/to/reaction-collection.csv --out-dir /path/to/empty-figures \
  --aspect-ratio 4 --width-inches 8
```

输入必须包含 `activation_gibbs_free_energy_kcal_mol`、`ene_id` 和 `diene_id`。
a 为总体活化自由能，b 为路径值减去 `(ene_id, diene_id)` 组均值的残差。
所有 CSV 行按路径等权保留，单路径组贡献零残差；不绘制排除单路径组的曲线。
横纵比指整个画布，输出不使用 tight 裁切，导出 SVG/PDF/PNG/TIFF 和统计、图注与来源记录。
该入口只验证所提供 CSV 的字段和有限数值，不要求五折模型结果包。

当前采样套件先导出快照，再在分析环境渲染：

```bash
uv run --project analysis --locked python -m analysis.sampling.replay_sampling_panels --help
uv run --project analysis --locked python -m analysis.sampling.reaction_collection_distribution --help
uv run --project analysis --locked python -m analysis.sampling.threshold_cost \
  --run-dir /path/to/sampling-root --out-dir /path/to/new-threshold-report
```

阈值成本报告读取已校验快照，对 MAE ≤ 5、4、3、2、1 取首次达标 checkpoint 的累计核时，
不插值，不用后续最优误差回填。五个种子均达标才报告均值、样本标准差和成本节省率；
未齐的单元格只报达标数量，不将未达到当作零或只平均成功种子。首次达标不代表持续达标。
核时包括初始化采样，排除 SAR 训练和 agent 开销；逐种子记录和图表一并导出。

三空间投稿曲线读取 `replay_sampling_panels` 导出的已校验快照：

```bash
uv run --project analysis --locked python -m analysis.sampling.publication_curves \
  --snapshot /path/to/snapshot.json --pool-pairs 1600 --out-dir /path/to/new-curves
```

`--pool-pairs` 必须填入该实验完整采样池的底物对数。虚线采用 LHS 五种子全量采满后
MAE 的均值作为经验参照，不代表纯计算误差或理论下界。曲线使用同成本下最近已观测
checkpoint 的均值和样本 SD，不外推；实线表示五种子，点线表示种子不足的区段。
三空间合图和独立图的完整画布均为 4:1.5，导出可编辑 SVG、PDF、600 dpi PNG/TIFF，
附曲线数据、逐种子首次达标成本、方法文献和适配说明、图注及快照哈希。

智能采样路线审计使用 `python -m analysis.sampling.route_audit --snapshot /path/to/snapshot.json
--out-dir /path/to/new-audit`。只纳入快照中下一轮结果已完成的决策，核对接受响应、输入哈希、
入选 ID 与下一轮实际揭示结果；重放本轮公式及前轮公式在当前候选池上的排序。
初始化和控制器全选剩余候选不计作智能体决策。分阶段统计先在各 seed 内平均，再按可用
seed 等权汇总，并保留每阶段轮数与种子数；部分轨迹的后期统计不冒充完整五种子结果。

反应集合分析必须显式提供 `--ground-truth`、`--expected-pairs` 和至少一个 `--project NAME=EXPORT_DIR`；数量从输入计算，不读取历史运行默认值。

结果核验、训练输入准备和固定资产索引仍使用 `python -m tricycle_sar results verify|prepare|assets`。
结果包只保存科学结果和图表，复用本目录的分析实现与环境。实验报告与图表写入 Git 忽略的 `reports/` 或 `artifacts/`，不纳入源码提交。

已完成 benchmark 的逐折 MAE 箱线图（每点一折）使用：

```bash
uv run --project analysis --locked python -m analysis.sar boxplot \
  --run-dir /path/to/benchmark --prediction-dir /path/to/collected-predictions \
  --out-dir /path/to/empty-boxplot --xscale log --aspect-ratio 2 --width-inches 7.2
```

入口调用 SAR 的 `collect`，核对冻结输入、划分、完整 OOF 身份与目标并重算指标，
不依赖已变更的当前执行后端；保留 run 中的原方法绑定供审计。
预测目录使用 `predictions.csv` 或既有 compact worker 目录结构，必须覆盖全部模型五折。
图中箱体为四分位距，中线为中位数，须为 1.5 IQR 范围内的极值，所有折均显示散点；
默认沿用 GT 分布图的蓝紫色填充、Arial 字体、深色细轴线、无网格和短标题，
使用对数 MAE 轴和 2:1 完整画布；不使用 tight 裁切。完整统计说明保存为
`figure-caption.txt`，每个箱线右侧标注五折原始 MAE 的均值 ± 样本标准差（ddof=1），
精确数值另存数据表；标注位于最右折点之后，避免遮挡数据。
对数轴只改变显示尺度，箱体及均值/样本 SD 仍由原始 MAE 计算；零值不能平移或删除，
需显式使用 `--xscale linear`。另保存逐折指标、输入哈希和 PNG/PDF/SVG/TIFF，
位图为 600 dpi，矢量文字可编辑。该分析不修改已提交的训练协议。
