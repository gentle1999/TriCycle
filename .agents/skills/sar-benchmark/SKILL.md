---
name: sar-benchmark
description: Prepare and publish compact, reproducible grouped five-fold SAR experiments using shared current backends.
---

# SAR 五折实验

`models` 只接受当前注册表的具体后端名称。Chemprop 使用 `chemprop_cgr` 和
`chemprop_multicomponent`；需要同时比较时分别列出，不使用模型别名。

## 记录约定

必须遵循 [SAR 模块的实验记录与归档](../../../packages/tricycle-sar/README.md#实验记录与归档)。新实验必须使用包内已通过 smoke 的模型后端，不能复制第二套模型代码、安装配方或逐折文件清单。模型、数据版本和 fold/seed/round 只记录共同定义与实际差异；不新增平行的 scope、manifest 或摘要。

唯一实现位于 `packages/tricycle-sar/src/tricycle_sar/`；`backends/catalog.json` 只组合可复用 environment/run，直接维护经过 smoke 的初始化和训练/推理入口。数据与统一划分分别保存不可变绑定；数据规模不属于训练约束。模型和折数从实验配置与 registry 读取，按生成的任务表核对覆盖，不在技能中维护固定数量。

## 准备与核验

复制并填写 [`example-sar-benchmark.toml`](../../../configs/example-sar-benchmark.toml)：选择模型，填写本次数据与已有划分的路径、SHA-256，并让 `training_protocol.reference` 指向当前后端引用。路径相对于请求文件解析；模板不绑定本机报告或历史实验数据。

`dataset`、`split` 中的未知字段会被拒绝。已有划分的 `split.path` 与 `split.sha256` 必须同时填写且有效；路径缺失、内容变化或字段错误时修正输入，不通过删掉绑定重新生成划分。两项均未提供只适用于按所选协议生成新实验划分。

```bash
uv run --all-packages python -m tricycle_sar benchmark prepare --config /path/to/filled-benchmark.toml --run-dir /path/to/new-run
uv run --all-packages python -m tricycle_sar benchmark check --run-dir /path/to/new-run
```

prepare/check 是本地操作，不联网、读凭据、安装、训练或提交。prepare 需要本地 NumPy，已有非空 run 不覆盖。共同实验定义和派生输入校验只写一份 `run.toml`；check 不生成另一份摘要。已有固化划分必须直接复用，不能重新随机生成替代已发布结果。

同一底物组所有路径在同一外折；validation 仅来自该外折训练部分，全部路径恰好测试一次。目标为 `G_T_activate`、kcal/mol；其他能量/耗时不作输入特征。以冻结 GT 重算指标，五折 MAE 均值/样本 SD 与 pooled OOF 指标分别报告。不用未完成折拼成完整比较，不按测试误差挑选重跑结果。

## 训练任务

需要 smoke 时，CPU、分子和原生模型通过对应 `tricycle_sar.preparation` 模块的 `--include-smoke` 准备；RxnResid/ReactionT5 使用 `tricycle_sar.preparation.smoke`。前向/反向 probe 与完整训练的验收范围不同，不能将 probe 成功记为完整训练和推理通过。probe 和环境检查的输入哈希必须在 `bohr.batch.prepare` 展开后的实际上传目录核验，包括重命名后的 `__bohr_task_script.sh`。

使用 `tricycle_sar.submission` 和 `tricycle_sar.preparation`，自动将共同字段提取到 `task_defaults`，`task_specs` 只写差异。完整 worker TOML、原生 snapshot/split/bundle manifest 仅在执行准备目录派生，不作为提交结果或第二套协议。默认只准备正式训练请求；新增 smoke 必须显式 `--include-smoke`，复用已验证的方法不自动重做 smoke。

Bohr 项目与任务组由调用方配置：`requests` 及各批任务、smoke、环境准备 CLI 必须传入 `--group-config /path/to/group.toml`，Python API 使用 `group_config=Path(...)`。配置含 `project_id`、`job_group_id`，可复制并填写 [`example-sar-bohr-group.toml`](../../../configs/example-sar-bohr-group.toml)；没有隐式任务组默认值。全模型准备使用 `--job-group-name` 创建新任务组绑定；不要把模型方法绑定与调度配置混在一起。

镜像、源码 revision、资产校验值、参数、初始化入口与执行器均读取[当前后端配置](../../../packages/tricycle-sar/src/tricycle_sar/backends/catalog.json)及其引用的配置/清单；技能不保存具体绑定值。不能据节点错误自行重装 Torch/CUDA、升级依赖或改建环境。当前共用代码直接维护，常规修复不复制历史实现或新增 catalog 版本；明确修改后同步更新受影响的配置绑定并验证，未知哈希漂移仍拒绝。

共用初始化入口对临时网络错误最多执行 3 次安装/下载命令，退避 5 秒、15 秒，pip 单次 450 秒，共享 1,800 秒初始化预算；复用 pip 缓存及已成功安装内容。默认镜像源返回空版本列表时可切官方源确认一次；官方源仍无版本或其他确定性错误不重试。GPU 初始化先核对实际 CUDA 可用性；失败标记 `inspect_error`，从预检事件核对 Python/Torch 路径、宿主驱动和实际加载的 CUDA 库，不能仅凭“driver too old”等报错判断节点故障或换节点；网络重试耗尽标记 `retry_job`，其他故障标记 `inspect_error`。从 `status.csv` 的 `initialization_retries/failure_reason/next_action` 和 `worker.log` 核对，不把动作建议当成已经重提任务；训练/推理不做无条件重跑。

ReactionT5 必须随任务交付固定本地权重/tokenizer/config；MolCLR 严格使用固定预训练权重，UAM 保持其固化从头训练方式。缺失资产或错误哈希时停止准备，不让 worker 从 HF 在线拉取。大资产按固定哈希外置。

已授权执行通过 Bohr/内网适配器；prepare/check 不产生外部副作用。已有 job ID 进入状态/下载，不盲目重提；失败重跑新建 run，保留成功、排队及运行任务。模型升级后的采样新开轨迹，不混用 checkpoint。采样和全池代理模型从无标签 prior 冻结统一 route 词表，同轨迹训练/refit/预测保持一致。

## 结果归档

结果 manifest 使用 `backend.sha256` 保存计算来源；缺失该字段或携带旧哈希字段时停止核验，
不自动迁移或改写已有结果。来源哈希不要求等于当前后端哈希。

具体实验的结果包、图表和复现说明保存在 Git 忽略的 `reports/` 或 `artifacts/`，不纳入源码提交；通用文档只描述接口与验收规则。

完成全部折并核验后使用模块导出：

```bash
uv run --all-packages python -m tricycle_sar benchmark publish --run-dir /path/to/completed-run --out-dir /path/to/result-package
```

只导出数据、统一划分、一份合并 OOF 预测、指标、合并的方法绑定及协议引用；可添加科学图表与复现说明。全部模型和 fold 的预测写入 `results/oof-predictions.csv`，保留模型输出标签用于舍入核验，逐折指标从同一表重算。worker 默认只输出合并 predictions/metrics/status，使用一份 worker.log；checkpoint 必须按需显式保留，采样轨迹需保留后续 refit/infer 所需文件。正式结果的后端引用合入同一 manifest，不另写 protocol-ref。不得归档 `results/raw/` 或其他逐折预测副本；原始 worker 预测留在 `runs/`。不得递归复制 run，或加入 `provenance/`、`inputs/`、任务回执、安装/设备/阶段日志和 checkpoint。同方法的适用折合并为一行，不另存逐折 JSON。

结果包不包含 `scripts/` 或 `reproduction/`。核验、资产索引和输入准备调用 `python -m tricycle_sar results verify|assets|prepare --result-dir /path/to/result-package`，复用 `tricycle_sar.results` 和 `tricycle_sar.training_inputs`。离线分析与绘图调用 `python -m analysis.sar render|distributions`，实现与依赖锁共用根目录 `analysis/`，不复制到各实验；项目包不导入分析代码。输入准备复用结果包的 model/fold、数据和划分，统一使用当前后端；派生文件写到报告外。冻结成绩可独立复算，不承诺恢复原上传目录或重新训练逐位一致。

当前新任务通过 `backends/catalog.json` 绑定共享环境引擎和执行入口；共同 core 文件按相同哈希随任务交付。只维护当前 catalog、源码与方法，不保留历史实现或兼容变体。结果中的方法 ID 仅记录计算来源；`results verify` 独立复算科学结果，`results prepare` 始终使用当前后端生成新输入，不执行训练或提交。

GT 总体活化自由能与底物组残差绘图复用 `python -m analysis.sar distributions --result-dir /path/to/result-package --out-dir /path/to/distributions`，并使用根目录 `analysis/` 隔离环境。总体及组内残差按路径等权，组内残差为路径值减去 `(ene_id, diene_id)` 组均值；单路径组保留零残差，另展示多路径组。组均值残差按组等权、中心取全部组均值的平均值。使用总体方差 ddof=0，保留全部尾部；不得将这些残差标为模型误差，或将路径加权组内方差混同各组方差的等权平均。输出在报告外，生成 PNG/PDF/SVG 与统计 JSON，拒绝覆盖非空目录。

科研绘图技能重绘时使用同一命令的 `--style publication`，不另建绘图源码树或安装环境。主图为 183 mm 三面板直方图，ECDF 为辅助图；导出 SVG/PDF/PNG/TIFF 及图注、alt text、规格、来源哈希、文字/面板布局。审计导出的文字边界、字体和面板对齐后再标记完整性通过；未经期刊指南核验不宣称期刊合规。

`sar.train/infer/finetune` 使用共享命令 builder，镜像和机器类型匹配后端。本地输入目录中的源码与固定归档一致；规划记录输入哈希，提交再次核验。单任务执行复用 `single_job.py` 与 `sampling_runtime` 的共享初始化和源码，缺失输入的 dry-run 计划不能直接提交。累计标签训练按全 train 划分选择 from-scratch 模式，预测按逐路径身份校验覆盖。
