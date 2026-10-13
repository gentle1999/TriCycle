# Bohr 模型运行环境

## 配置来源

模型环境和平台资源由配置选择，不能从通用文档中的安装示例推断。当前入口如下：

| 工作流 | 配置与准备入口 |
| --- | --- |
| SAR 多模型、RxnResid 单任务及采样 | [catalog.json](../packages/tricycle-sar/src/tricycle_sar/backends/catalog.json)、[环境配置](../packages/tricycle-sar/src/tricycle_sar/backends/environments/)、[SAR 包](../packages/tricycle-sar/README.md) |
| RitS 训练/微调/推理 | [模型资产](../packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml)、[请求示例](../configs/example-rits-train-request.toml)、[RitS 包](../packages/tricycle-rits/README.md) |
| DPA-Berny 与 Gaussian | [引擎请求示例](../configs/example-dpa-berny-request.toml)、[BohrSubmit](../packages/tricycle-bohrsubmit/README.md) |
| 采样套件与整条轨迹 | [配置索引](../configs/README.md)，通过 SAR 共用接口交付模型 |

Bohr GPU 请求使用当前配置中的统一机型；DPA 能量与梯度必须在 GPU 上执行，纯 Gaussian 和指纹模型使用 CPU。RXNGraphormer 使用 catalog 指定的内网 SSH/Docker 执行器。不要用全局机器默认值覆盖这些资源边界。

## 解释器、环境与源码

SAR 的 `workers/unified.sh` 进入共享执行器；环境引擎按所选 profile 选择解释器和初始化方式。继承镜像的环境校验基础 Python/Torch，再创建可继承镜像依赖的任务环境；CPU 环境按自身配方隔离依赖。实际 Python 路径、Torch/CUDA 版本与依赖来自执行记录，不能假定 `python3.12` 或某个系统路径一定对应模型环境。

源码、配置、数据和 checkpoint 按准备接口交付，镜像不能替代源码绑定。RitS 的模型来源与权重位置由资产 TOML 指定；微调使用 `finetune_from`，保留优化器状态的恢复使用 `resume`。ReactionOpt 的 TS pipeline 还要求请求绑定包含 `scripts/run_ts_pipeline.py` 的源码，模型资产的 revision 不能替代 pipeline 来源证明。

固定资产必须在本地存在并通过校验。ReactionT5 的权重、tokenizer 和 config 随任务提供；MolCLR 使用指定预训练权重。运行失败时先保留诊断，不通过升级依赖、替换 checkpoint 或切换训练方式让检查通过。

## 本地准备与设备诊断

只读镜像清单和 SAR 环境任务均由包内入口准备：

```bash
uv run --all-packages python -m tricycle_sar.preparation.inventory \
  --config configs/example-sar-inventory.toml --out-dir /path/to/new-inventory
uv run --all-packages python -m tricycle_sar.preparation.environment \
  --model rxnresid --out-dir /path/to/new-environment-check \
  --group-config /path/to/group.toml
```

这些命令只准备本地输入；生成的 Bohr 请求再走 [batch prepare/submit/status/download 流程](../packages/tricycle-bohrsubmit/README.md#cli-示例)。任务组配置见 [example-sar-bohr-group.toml](../configs/example-sar-bohr-group.toml)。提交需在已有授权范围内显式进入 live 路径。

GPU 初始化应在实际任务容器、实际模型解释器中核对：

- Python/Torch 路径、CUDA runtime、`nvidia-smi` 的驱动和设备信息；
- 驱动 API 版本与实际加载的 CUDA 动态库；
- 实际 CUDA 运算及模型进程的设备证据。

节点模式下看到的驱动不是另一个 job 的环境证明；申请了 GPU 或 `torch.cuda.is_available()` 为真也不等于模型实际完成 GPU 计算。SAR 预检日志和错误下一动作见[加速器执行约定](sar-accelerator-execution.md)。不能把反复提交到偶然可用节点作为兼容性验收。

## Smoke 验收与恢复

分别报告输入准备、环境检查、模型前后向 probe、训练后预测和完整工作流的结果。probe 成功不能替代完整训练/推理；RitS 推理成功也不能替代 ReactionOpt 的 TS 后处理验收。

任务完成后查询状态、下载产物，核对预期预测/结构、数值有效性、输入覆盖以及 GPU 设备证据。DPA 同时核对能量/梯度请求和优化产物；纯 Gaussian 按 CPU 任务验收。实际 job ID、驱动版本、耗时与成功/失败记录只写入该 run，不回填到通用指南。

环境初始化对临时网络错误执行有限重试；确定性的依赖、资产或 CUDA 错误需先检查。训练和推理不自动整段重跑，平台提交结果未知时先核对回执。执行计算不授权修改原定协议。具体重试规则由 [SAR 包](../packages/tricycle-sar/README.md#精简任务输出)和配置负责。
