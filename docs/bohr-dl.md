# Bohr 深度学习运行环境

## 当前镜像

RxnResid 和 RitS 训练、微调、推理统一使用：

```text
registry.dp.tech/dptech/dp/native/prod-23278/dl:260828
```

该镜像包含系统 Python 3.12、PyTorch、PyG 及其加速库、RDKit、Open Babel，以及模型任务
需要的公共运行时。镜像不包含 RitS、RxnResid 或其他任务源码。

每次任务将当前代码仓库、`pyproject.toml`、任务原生配置、数据和 checkpoint 作为 artifact
上传。对于当前 `bohr` CLI，`input_directory` 应指向包含这些内容的本地任务目录；请求中的
`code_artifact`、`dataset`、`checkpoint` 和 `input_artifacts` 同时用于审计输入。Bohr 直接执行
输入目录中的 bash entrypoint，例如：

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
python3.12 -m pip install --no-cache-dir --editable .
exec python3.12 train.py
```

因此任务代码目录必须包含 `pyproject.toml`；任务原生配置保持模型项目要求的格式，例如
RitS 的 Hydra YAML，不需要转换成 TriCycle TOML。该 entrypoint 不创建或激活虚拟环境。

## 本地镜像 smoke test

先登录镜像仓库并拉取镜像：

```bash
docker login registry.dp.tech
docker pull registry.dp.tech/dptech/dp/native/prod-23278/dl:260828
```

检查公共依赖和 PyG 扩展：

```bash
docker run --rm \
  registry.dp.tech/dptech/dp/native/prod-23278/dl:260828 \
  python3.12 -c "
import torch
import torch_geometric
import pyg_lib
import torch_scatter
import torch_sparse
from rdkit import Chem
from openbabel import pybel
from torch_scatter import scatter
x = torch.tensor([1., 2., 3.])
i = torch.tensor([0, 1, 0])
print('torch:', torch.__version__)
print('cuda:', torch.version.cuda)
print('pyg:', torch_geometric.__version__)
print('scatter:', scatter(x, i, dim=0, reduce='sum'))
print('runtime imports: OK')
"
```

有 NVIDIA GPU 时，再执行 CUDA 检查：

```bash
docker run --rm --gpus all \
  registry.dp.tech/dptech/dp/native/prod-23278/dl:260828 \
  python3.12 -c "
import torch
assert torch.cuda.is_available()
print(torch.cuda.get_device_name(0))
print(torch.cuda.get_device_capability(0))
"
```

## 验证任务代码安装

将本地 RitS 仓库挂载到 `/workspace/job`，验证可编辑安装和基础导入：

```bash
docker run --rm --gpus all \
  -v "$PWD/RitS:/workspace/job" \
  registry.dp.tech/dptech/dp/native/prod-23278/dl:260828 \
  bash -lc 'cd /workspace/job && python3.12 -m pip install --no-cache-dir --editable . && \
    python3.12 -c "import megalodon; print('"'"'RitS install/import: OK'"'"')"'
```

RxnResid 使用同样的方式，将挂载路径替换为 RxnResid 仓库，并把最后的 Python 命令替换为
其实际包导入或最小推理入口。

模型级测试应使用一个样本或极小数据集：推理先验证能产生预测文件，训练只运行一个 batch
或一个 epoch，并确认 checkpoint、日志和输出 artifact 均能生成。

两种上游微调协议不能互换：RitS 使用
`scripts/train.py ... finetune_from=<checkpoint>`，只加载权重并启动新的 optimizer；
RxnResid 使用 `train.py --refit --checkpoint <selection-checkpoint>`，从 checkpoint
读取 config/fold/split，在 train+valid 上以新的 optimizer 重拟合。后者不应再传
`--config` 或 `--fold-index`。

RitS 的实际训练、微调和推理参数以其[官方仓库说明](https://github.com/gentle1999/RitS#usage)
为准；TriCycle 只负责把命令、任务目录和 artifact 交给 Bohr。

## RitS 版本与权重

当前组件锁定官方 RitS 仓库提交：

```text
https://github.com/gentle1999/RitS@6d8aa0704c21d2798326a7e070491abf438ca292
```

默认使用官方发布目录中的 `rits.ckpt`（RGD1-ChEMBL-extended 模型）。请先将文件放到
TriCycle 根目录：

```text
artifacts/rits/rits.ckpt
```

当前文件的 SHA-256 为
`2c6fdacc13a304cb8bb4030881ff4198de647d8f8f7fdaa878414e2514be0668`；组件清单会在校验时使用它。

再复制到实际上传的任务目录，使其满足 RitS 官方入口的相对路径：

```bash
mkdir -p /path/to/RitS/data
cp artifacts/rits/rits.ckpt /path/to/RitS/data/rits.ckpt
```

组件级 TOML 清单和官方 checkpoint 来源见
[`packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml`](../packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml)。
`ts1x_rits.ckpt` 属于另一套 Transition1x 模型，不作为当前默认权重。

## RxnResid 版本与入口

RxnResid 组件固定使用：

```text
https://github.com/gentle1999/RxnResid@9deebde39b38df1085dbac3a379c2a59edd7c97d
```

其官方任务入口为 `train.py` 和 `predict.py`，默认项目配置为
`configs/rxnresid.yaml`。任务目录应携带该仓库的代码、数据、配置和 checkpoint；
`tricycle-sar` 仅在计划中记录仓库 provenance，并通过上传目录中的 bash entrypoint 调用这些入口。
当前 `predict.py` 还会输出 `aleatoric_variance`、`epistemic_variance`、`predictive_variance`、
对应标准差以及 `lower_95`/`upper_95`。主动学习默认使用 `epistemic_variance` 排序；这些字段
会随 SAR plan 的 `uncertainty_fields` 元数据一并记录。

## Bohr 任务测试

在环境变量中指定镜像：

```bash
export BOHR_ML_IMAGE=registry.dp.tech/dptech/dp/native/prod-23278/dl:260828
```

先用 `rits.infer` 或 `sar.infer`（RxnResid）的 TOML 请求执行 dry-run，检查 `image`、command、资源和
输入 artifact；确认无误后再显式使用 `--live` 提交一个最小任务。任务完成后依次查询状态
并下载输出，重点确认：

- worker 能安装随任务上传的最新代码；
- CUDA、PyG 加速库和模型包可以导入；
- 输出目录、checkpoint 或预测文件被声明并成功下载；
- `run.toml` 中记录了远程任务 ID 和下一步动作。
