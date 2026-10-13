---
name: rits-finetune
description: "Plan or explicitly submit a RitS fine-tuning job to Bohr from a TOML request."
---

# RitS 微调

RitS 微调使用上游 `scripts/train.py` 的 Hydra override
`finetune_from=<checkpoint>`。它只加载 checkpoint
权重，并以当前配置创建新的 optimizer、scheduler 和训练运行；不要把它和断点续训
`resume=<checkpoint-or-run-directory>` 混为一谈。

源码来源与预期版本读取[模型资产配置](../../../packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml)的
`model.repository`、`model.revision`，把匹配的完整 commit SHA 写入请求的
`source_revision`，不要仅记录可移动的 `main`。默认预训练权重按配置的 `checkpoint_sha256` 校验；用户提供的训练 checkpoint 按其 artifact/运行记录校验，不拿默认权重的 hash 限制所有 checkpoint。
技能不绑定具体 commit 或校验值。请求必须包含代码目录、RitS 数据集
artifact、checkpoint artifact、`input_directory`、worker 镜像和实际 command。数据集
根目录、输出目录、epoch 等保持为 RitS 原生 Hydra 配置或 override。

`tricycle_rits.build_finetune_command(checkpoint=..., overrides=...)` 返回原生 Python/Hydra argv，
不生成 `run.sh`，也不安装依赖。需要启动脚本时由任务准备入口交付，并把构造出的命令放入脚本；
只有目录中实际存在该脚本时，提交命令才可写成 `bash run.sh`。

解释器、镜像与依赖配方读取所选任务配置和对应源码。按包文档核验 Torch/CUDA、PyG 编译依赖、
日志依赖和 GPU 可用性；不从技能复制固定 Python/CUDA 版本或临时替换镜像中的 Torch。

数据库导出的 mapped reaction/TS XYZ CSV 是源数据，不是预处理训练目录。按所选 revision 的
预处理器生成训练输入；如果它要求 AAM 列、正反反应成对、共享 TS XYZ 或排除索引，先核对其实际
参数与数据含义再转换。验证 atom-map、元素与 XYZ 顺序，保留拒绝清单，不能将文件改名当作预处理。

先规划并确认 artifact、`source_repository`、`source_revision` 和 command：

```bash
uv run --package tricycle-agent tricycle-agent plan \
  rits.finetune configs/example-rits-finetune-request.toml --run-id rits-finetune-smoke
```

真实提交必须显式使用 `run ... --live`。默认 dry-run 不读凭据、不联网、不上传、不提交
Bohr；已提交任务使用 `bohr.job.status` 和 `bohr.job.download`，不要重复创建任务。

## 内网 Docker/SSH 执行

用户选择内网执行时使用 `rits.ssh.finetune`；Bohr 调度失败本身不改变已选执行器。SSH 仅负责将 `bundle_dir` 上传到内网
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
