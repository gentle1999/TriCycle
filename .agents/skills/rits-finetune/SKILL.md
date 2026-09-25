---
name: rits-finetune
description: "Plan or explicitly submit a RitS fine-tuning job to Bohr from a TOML request."
---

# RitS 微调

RitS 微调使用上游 `scripts/train.py` 的 Hydra override
`finetune_from=<checkpoint>`（`init_from_ckpt` 是兼容别名）。它只加载 checkpoint
权重，并以当前配置创建新的 optimizer、scheduler 和训练运行；不要把它和断点续训
`resume=<checkpoint-or-run-directory>` 混为一谈。

代码来自 `https://github.com/gentle1999/RitS` 时，必须把实际完整 commit SHA 写入请求的
`source_revision`；不要仅记录可移动的 `main`。当前已核对的 fork revision 为
`b4fc883a0e97d8946731c4df12e3585cd4e98fd5`。请求必须包含代码目录、RitS 数据集
artifact、checkpoint artifact、`input_directory`、worker 镜像和实际 command。数据集
根目录、输出目录、epoch 等保持为 RitS 原生 Hydra 配置或 override。

推荐使用 `tricycle_rits.build_finetune_command` 生成 command，形如：

```text
bash run.sh
```

The current fork expresses Python dependencies as version ranges in both `pyproject.toml` and
`requirements.txt`; it does not provide a complete lockfile. For Bohr images that supply PyTorch,
inspect and log `torch.__version__` and `torch.version.cuda`, install the revision's `requirements.txt`
with PyG wheels matching that exact Torch/CUDA/Python combination, then install the source with
`python3.12 -m pip install --no-cache-dir --no-deps --editable .`. If the fork's PyG wheel URL does
not match the selected image (for example, its `cu132` URL versus a `cu130` image), use an explicit
task-local requirements override or select a matching image; do not mix compiled CUDA variants.
Verify `torch_geometric`, `torch_scatter`, and `torch_sparse` imports and CUDA availability before
preprocessing. Do not download or replace the image's Torch when its version is within the fork's
declared range. The current training script creates a Lightning `TensorBoardLogger`; ensure
`tensorboard` or `tensorboardX` is installed even though the fork's runtime requirements omit it.

When starting from mapped-reaction/XYZ source data, run the revision's
`data_processing/prepare_ts1x_for_training.py` before training. Keep each forward/reverse SMARTS pair
adjacent and duplicate the same TS XYZ block for both directions; disable the preprocessor's TS1x-specific
default `ban_index` entries for a different source dataset with `--ban_index -1`. Finally run:

```text
python3.12 data_processing/prepare_ts1x_for_training.py \
  --ts_data data/rits_dataset/raw/mapped_transition_states.xyz \
  --rxn_smarts_file data/rits_dataset/raw/mapped_reactions_AAM.csv \
  --save_dir data/rits_dataset \
  --ban_index -1

python3.12 scripts/train.py \
  --config-path=conf --config-name=rits \
  data.dataset_root=./data/rits_dataset \
  finetune_from=./data/rits.ckpt
```

先规划并确认 artifact、`source_repository`、`source_revision` 和 command：

```bash
uv run --package tricycle-agent tricycle-agent plan \
  rits.finetune configs/example-rits-finetune-request.toml --run-id rits-finetune-smoke
```

真实提交必须显式使用 `run ... --live`。默认 dry-run 不读凭据、不联网、不上传、不提交
Bohr；已提交任务使用 `bohr.job.status` 和 `bohr.job.download`，不要重复创建任务。

## 内网 Docker/SSH 执行

当 Bohr GPU 不可用时，改用 `rits.ssh.finetune`。SSH 仅负责将 `bundle_dir` 上传到内网
GPU 主机并管理生命周期；训练命令必须在 `container_image` 指定的 Docker 镜像中执行。
镜像需预装与 RitS fork 兼容的 Python/PyTorch/CUDA 运行时及所需系统依赖；远端 Docker
daemon 必须可用，并配置 NVIDIA Container Toolkit。默认使用 `--gpus all`，可用
`container_gpus = "0"` 或 `"0,1"` 选择设备。训练日志通过远端 Docker context 的 `logs`
操作读取，任务状态通过容器 `inspect` 查询；执行器会固定注入 `WANDB_MODE=disabled` 和
`WANDB_DISABLED=true`。

SSH 认证沿用本机已配置的 OpenSSH agent/config；请求中不得放私钥或密码。目标主机必须事先
出现在 `known_hosts`，SSH config 应设置非交互认证和严格 host-key 校验。dry-run 不读 SSH
config/密钥、不访问 Docker、不联网。

```bash
uv run --package tricycle-agent tricycle-agent plan \
  rits.ssh.finetune configs/example-rits-ssh-finetune-request.toml \
  --run-id rits-internal-smoke

uv run --package tricycle-agent tricycle-agent run \
  rits.ssh.finetune configs/example-rits-ssh-finetune-request.toml \
  --run-id rits-internal-smoke --live
```

提交后使用 `rits.ssh.status`、`rits.ssh.logs`、`rits.ssh.download` 管理同一 `job_id`；
`rits.ssh.terminate` 会发送 Docker stop/SIGTERM，必须显式传 `--confirm`。下载时只列出
需要的相对输出路径，不要把完整的代码/数据任务目录复制回本地。示例请求和分能力说明见
`.agents/skills/rits-ssh-finetune/SKILL.md`。
