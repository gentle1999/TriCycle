# SAR worker 加速器执行约定

执行脚本把环境安装、模型加载和训练分成可观察、有限时长的阶段。GPU 机型申请成功或 `torch.cuda.is_available()` 为真，不再被视为模型使用了 GPU 的证明。不同模型的 smoke/训练结果按 run 记录，不能由基础镜像检查替代。

## 已接入的入口

当前模型共用包内 `backends/workers/unified.sh`，由 `execution.py` 按模型选择 runner。环境配方、运行文件与实际机型通过 [当前后端配置](../packages/tricycle-sar/src/tricycle_sar/backends/catalog.json) 绑定；不提供模型名或 `tools/` 下的 shell 转发入口。

| 模型 | 包内 runner | 资源 |
| --- | --- | --- |
| 指纹 RF / Extra Trees / Ridge | `classical_runner.py` | Bohr CPU，CUDA 显式隐藏 |
| UAM / MolCLR | `molecular_runner.py` | Bohr GPU，实际模型进程强制 CUDA |
| Chemprop CGR / multicomponent、T5Chem | `backends/native_runner.py` | Bohr GPU，训练 CLI 在其专用 Python 中运行并受 CUDA 检查约束 |
| ReactionT5、RxnResid | `backends/complete_runner.py` | Bohr GPU；设备证据随 run 归档 |
| RXNGraphormer | `backends/complete_runner.py` | 固定内网 SSH/Docker 执行器 |

## 执行流程

1. 本地准备时验证实际写出的 TOML、输入哈希和机型。上传包包含执行支持代码及其 SHA256，不改写旧 run。
2. worker 验证上传运行时代码哈希，清理代理与外部 Python 导入路径；CPU worker 强制清空 `CUDA_VISIBLE_DEVICES`。
3. `sar_stage.py` 监督安装与训练子进程，实时转发日志，每 30 秒记录阶段与 GPU/进程显存信息。GPU 阶段先检查 `nvidia-smi`。
4. 在模型专用解释器内运行 `sar_accelerator_exec.py`。它保留原模型、数据划分、损失和优化器，仅观察设备与训练事件。
5. 对有参数的实际模块检查参数和输入张量的设备；反向传播的损失张量必须位于对应设备。首次优化器更新必须有同设备、有限且非零的梯度。
6. GPU 模型进程完成时必须同时具有真实前向、反向和优化器更新记录；smoke 不要求优化器更新，推理不要求反向。缺少记录或设备不符立即失败。

报告记录 PID、PyTorch 版本、设备名、CUDA 已分配显存、运行时代码哈希、首次训练事件和优化器步数。GPU 利用率采样只是诊断信息；不以某个瞬间的利用率阈值判定成功，也不承诺小模型持续满载。

## 超时与恢复

- 共用执行器的初始化 stage 总时限为 2400 秒；环境引擎内的初始化命令共享 1800 秒累计预算。pip 单次最多 450 秒，其他子命令最多 900 秒。
- 临时网络错误最多执行 3 次命令，退避 5 秒、15 秒；确定性错误不重试。详细规则见 [SAR 初始化与输出](../packages/tricycle-sar/README.md#精简任务输出)。
- 训练开始后 600 秒内必须出现真实反向记录；之后超过 600 秒没有训练进展也失败。
- CPU 单折总时限 7200 秒，GPU 单折 86400 秒；Bohr 请求的总时限仍是额外上限。
- 超时/信号传播至子进程组；退出时尽力保存已产生的模型文件和报告。不得覆盖旧验证文件或自动重提任务。

环境安装失败、任务返回非零、无训练事件、设备不符均不会产生“成功”状态。CPU 模型不加载 GPU 检查器，不因深度学习镜像带有 CUDA 库而启用 GPU。

## 验证记录

执行层的单元测试覆盖 CPU 优化器更新、CUDA 不可用、模型设备不符、缺少训练事件、旧证据复用、非零退出、进程组超时清理、TOML 与任务包生成。模型级 smoke 和正式训练状态属于具体 run 的实验记录，应在对应报告与 provenance 中核对；本文件只定义执行约束。

安装源按环境配置选择；默认镜像源遇到可恢复网络问题或空版本列表时可切到官方 PyPI，显式指定的源保持原值。安装尝试与 CUDA 预检证据写入 `worker.log`。CUDA 失败的下一动作是 `inspect_error`，不能仅凭报错文字自动换节点或改变 Torch/CUDA。

各模型的 Python、PyTorch、CUDA 和依赖版本以固定模型 recipe 与单次任务 provenance 为准；不要把共享 worker 的镜像版本当作所有模型的通用环境。
