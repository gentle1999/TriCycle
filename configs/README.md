# 当前配置

此目录只维护当前请求示例、实验协议和声明式绑定。运行回执、一次性调度设置及已被替代的实验配置保存在本地归档，不随源码提交。

| 用途 | 配置 |
| --- | --- |
| 插件能力请求 | `example-*.toml`；使用时填写本次输入、路径和资源 |
| 单批候选选择 | `sampling-baselines/*.toml` |
| 五基线与智能采样逐轮比较 | `dpa-unified-sampling-suite.toml` |
| 五基线整条轨迹 worker | `rxnresid-matched-sampling.toml` |
| 数据库四池导出 | `database-sampling-pools.toml` |
| 当前固定数据空间 | `frozen-sampling-space.toml`，只维护这一份当前 pin |
| SAR 五折基准与当前后端引用 | `example-sar-benchmark.toml`、`sar-training-protocols.lock.json` |
| SAR 调度与只读镜像清单 | `example-sar-bohr-group.toml`、`example-sar-inventory.toml` |

Bohr GPU 请求及 SAR catalog 的 GPU 环境使用统一机型；纯 Gaussian 与指纹模型使用 CPU，RXNGraphormer 使用内网执行器。具体机型只在请求和 catalog 中维护，不设置覆盖全部计算的全局 GPU 默认值。

采样实验配置保留各自 readiness、数据与成本账本的绑定；它们不是通用数据默认值。新实验应先核验输入，按所属技能准备独立配置，不自动把历史轨迹改绑到当前空间。SAR 准备命令要求显式 `--group-config`，不读取某次 smoke 的任务组或授权状态。

过时配置按原字节归档到 Git 已忽略的 `artifacts/config-archive/<时间>/`，清单记录原路径、SHA-256、归档原因和替代入口。归档中的相对路径仍以原位置解释；如需核验历史配置，应在隔离目录恢复对应布局。当前代码、测试及操作说明不依赖归档。原数据快照、任务记录和 checkpoint 保留在各自运行目录。

`codex-usage-alert.toml` 和 `decision-model-balance-alert.draft.toml` 是已有的机器本地告警配置，继续由 `.gitignore` 排除。
