# TriCycle Agent

TriCycle 是面向环加成反应探索的 Python workspace，连接候选生成、采样、Bohr 计算、反应数据库与 SAR/RitS 模型任务。组合应用注册能力和注入外部 adapter；领域实现由各包维护。

## 安装与本地检查

需要 Python `>=3.11` 和 `uv`。在仓库根目录执行：

```bash
uv sync --all-packages
uv run --all-packages tricycle-agent capabilities
uv run --all-packages tricycle-agent plan \
  rits.infer configs/example-rits-infer-request.toml --run-id rits-plan-demo
```

`plan` 只校验和生成计划。示例中的输入路径、项目与资源应按实际任务填写；规划成功不代表输入已就绪或模型已通过运行验收。

```bash
make check
make ci-test
make typecheck
```

`make check` 提供不依赖 uv 的基础检查；完整依赖与各子项目测试由 `make ci-test` 执行。检查范围见[开发说明](docs/development.md)。

## 包与入口

| 子项目 | 职责与主要入口 |
| --- | --- |
| [tricycle-core](packages/tricycle-core/README.md) | 公共类型、protocol、能力目录、运行状态、trace、文件和指标工具 |
| [tricycle-bohr](packages/tricycle-bohr/README.md) | 单任务计算和模型 job 契约、计划与注入的执行边界 |
| [tricycle-bohrsubmit](packages/tricycle-bohrsubmit/README.md) | 批量 staging、任务组、提交/查询/下载/恢复，以及 DPA-Berny/Gaussian |
| [tricycle-reaction-db](packages/tricycle-reaction-db/README.md) | 计算日志导入、数据库筛选、训练数据导出与底物身份适配 |
| [tricycle-sar](packages/tricycle-sar/README.md) | 多模型后端、环境初始化、训练/推理、基准与结果核验；`sar.*` 能力对应 RxnResid |
| [tricycle-rits](packages/tricycle-rits/README.md) | RitS 训练、微调、推理，以及 SSH/Docker 任务 |
| [tricycle-sampling](packages/tricycle-sampling/README.md) | 候选选择、文献基线、智能采集、采样回放与监督 |
| [tricycle-reactionopt](packages/tricycle-reactionopt/README.md) | 反应配置、DA 枚举与反应物/产物/RitS TS 三分支 profile |
| [tricycle-exploration](packages/tricycle-exploration/README.md) | 探索轮次记录、空间决策、RitS train/skip gate 与快照迁移 |
| [tricycle-agent](apps/tricycle-agent/README.md) | 能力注册、CLI、adapter 装配与持续探索 campaign |

领域逻辑位于 `packages/`；`tools/` 只保留开发、运维和示例编排。离线分析与绘图及其环境统一在 [analysis/](analysis/README.md)。技能唯一目录为 [.agents/skills/](.agents/skills/README.md)。依赖方向和执行边界见[当前架构](docs/architecture.md)。

## 配置与计算

[configs/](configs/README.md) 维护当前请求、实验绑定和示例。源码 revision、权重校验值、镜像和机型从配置或资产清单读取，文档不复制这些具体绑定。

- SAR 使用[后端 catalog](packages/tricycle-sar/src/tricycle_sar/backends/catalog.json)及其环境配置；RxnResid 的来源与入口另见[模型资产](packages/tricycle-sar/src/tricycle_sar/assets/rxnresid-model.toml)。
- RitS 的来源与权重见[模型资产](packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml)，Bohr 请求见 `configs/example-rits-*-request.toml`。
- Bohr GPU 资源按当前配置统一选择；DPA 必须使用 GPU，纯 Gaussian 和指纹模型使用 CPU。RXNGraphormer 使用配置指定的内网执行器。具体环境、设备检查与失败处理见[Bohr 模型环境](docs/bohr-dl.md)。

通用能力请求和 `run.toml` 使用 TOML；模型原生 YAML/JSON、SAR catalog、结果清单及采样控制器的 JSON 状态保持各自契约，不强制转换格式。

远程 Bohr 操作需要安装并配置 `bohr` CLI，adapter 策略见 [example-bohr.toml](configs/example-bohr.toml)。凭据通过进程环境或 Git 忽略的 `.env` 注入：

```bash
cp .env.example .env
```

按本机情况编辑配置，不提交密钥。常用变量包括 `BOHR_ACCESS_KEY`、`BOHR_BIN`、`BOHR_PROJECT_ID`、`BOHR_IMAGE` 和 `BOHR_ML_IMAGE`。请求显式值优先于环境默认值；不要用全局 `BOHR_MACHINE_TYPE` 将 CPU 任务改为 GPU。默认 dry-run 不构造 live adapter、不访问外部服务。

## 运行与恢复

```bash
# 本地候选选择，写入本次 run。
uv run --all-packages tricycle-agent run \
  candidate.select configs/example-sampling-request.toml --run-id sampling-demo

# 查询本地记录；恢复执行状态里指定的下一动作。
uv run --all-packages tricycle-agent inspect --run-dir /absolute/run
uv run --all-packages tricycle-agent resume --run-dir /absolute/run --live
```

`--live` 只用于已经授权的外部操作；破坏性能力另需 `--confirm`。已有 run 使用 `resume`，不重新初始化；提交回执不确定时先核对平台记录，避免重复任务。批量工作流和下载失败 worker 的部分产物见 [BohrSubmit](packages/tricycle-bohrsubmit/README.md)。

持续探索通过 `tricycle-agent campaign` 操作，从既有 Bohr 批次开始监控、下载、分析、选样和准备下一批。输入与恢复流程见 [exploration-campaign 技能](.agents/skills/exploration-campaign/SKILL.md)。采样回放的两个执行入口见[采样指南](docs/intelligent-replay.md)。

## 文档与运行产物

[文档索引](docs/README.md)提供接口、协议、操作和开发指南。默认运行目录为 `runs/YYYY-MM-DD/HH-MM-SS.ffffff±ZZZZ/<run-id>`，使用本机时区；显式目录与恢复路径按请求保留。

具体实验的成绩、数据统计、任务 ID、日志、图表和复现报告保存在 Git 忽略的 `runs/`、`reports/` 或 `artifacts/`，不进入源码提交。过时文档先按原字节归档到 `artifacts/document-archive/`，记录原路径和哈希，再从维护文档及 Git 跟踪中移除。通用指南不依赖本机归档；测试使用自建的小型数据，不依赖某次实验结果。

开发前阅读 [AGENTS.md](AGENTS.md) 和[代码结构技能](.agents/skills/code-structure/SKILL.md)。新增能力同步包 README、[插件契约](docs/plugin-contract.md)、技能和请求示例。
