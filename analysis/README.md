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

当前采样套件先导出快照，再在分析环境渲染：

```bash
uv run --project analysis --locked python -m analysis.sampling.replay_sampling_panels --help
uv run --project analysis --locked python -m analysis.sampling.reaction_collection_distribution --help
```

反应集合分析必须显式提供 `--ground-truth`、`--expected-pairs` 和至少一个 `--project NAME=EXPORT_DIR`；数量从输入计算，不读取历史运行默认值。

结果核验、训练输入准备和固定资产索引仍使用 `python -m tricycle_sar results verify|prepare|assets`。
结果包只保存科学结果和图表，复用本目录的分析实现与环境。实验报告与图表写入 Git 忽略的 `reports/` 或 `artifacts/`，不纳入源码提交。
