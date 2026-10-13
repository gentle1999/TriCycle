---
name: rits-ssh-finetune
description: "Stage and execute a RitS fine-tuning bundle in a Docker container on an internal GPU host over SSH."
---

# 内网 RitS Docker 微调

用户选择内网 Docker/SSH 执行时使用 rits.ssh.finetune。协议职责分离为：本机通过 OpenSSH/scp 上传
任务 bundle，内网主机 Docker daemon 启动 GPU 容器；训练脚本不会在 SSH 主机裸机上运行。
容器以 /workspace/job（或 container_workdir）挂载远端任务目录，训练 stdout/stderr
由 Docker logging driver 保留并通过 rits.ssh.logs 读取。
执行命令使用非登录 Bash（`bash -c`）以保留镜像定义的 PATH 等环境变量；不要依赖登录 shell 初始化。

所有 SSH 生命周期请求只接受 `RitsSshFinetuneRequest` 声明的字段，未知字段直接报错。
微调请求必须包含 host、remote_root、bundle_dir、command 和 container_image。GPU 默认
使用 container_gpus = "all"，也可使用 "0" 或 "0,1"。镜像必须在远端 Docker daemon
可访问的 registry 中，并匹配主机 NVIDIA driver / Container Toolkit；镜像需提供 Bash、
Python、RitS fork 依赖和兼容的 PyTorch/CUDA。提交前适配器会通过 Docker context 检查 docker info，但不猜测或
替换用户选定的镜像，也不隐式构建镜像。远端容器会设置 WANDB_MODE=disabled 和
WANDB_DISABLED=true。

远端 SSH 用户必须有权限访问 Docker daemon socket（通常属于 docker 组或使用 rootless
Docker）；本机 Docker CLI 通过 remote context 向远端 Docker API 发请求。

## 准备训练镜像

现有镜像配方见 [Dockerfile](../../../packages/tricycle-rits/docker/cu130/Dockerfile) 和同目录依赖清单，
具体基础镜像、Torch/CUDA 与依赖版本以这些文件为准。不能仅因仓库有配方就假定目标主机已有该镜像。
任务的 `container_image`、`docker_context`、主机和 GPU 选择读取 TOML 请求。

镜像构建是独立操作，仅在任务明确包含构建时执行；普通 finetune 不隐式构建或更换镜像。
构建时使用配置指定的 Docker context 与镜像标签，build context 为上述配方目录，源码、训练数据和
checkpoint 仍由 bundle 提供。配方中的 `RITS_DEPS_IN_IMAGE=1` 由支持该约定的 bundle 安装脚本消费，
SSH adapter 本身不会安装模型依赖。构建后核验解释器和关键依赖，实际启动容器后再核验 GPU。

认证由本机已配置的 OpenSSH agent/config 提供。不要把密码或私钥内容写入 TOML；目标 host key
必须预先加入 known_hosts，SSH config 应设置 BatchMode=yes 和 StrictHostKeyChecking=yes。
live 首次使用时，适配器通过 docker context ls 检查本地 context，并在 context 不存在时执行
docker context create remote --docker host=ssh://user@host；已有同名 context 指向不同 endpoint
时会报错，不会覆盖。默认 plan 不检查密钥、不访问 SSH/Docker、不发网络请求；真实上传和
启动仅由显式 run ... --live 执行。

    uv run --package tricycle-agent tricycle-agent plan \
      rits.ssh.finetune configs/example-rits-ssh-finetune-request.toml \
      --run-id rits-internal-smoke

    uv run --package tricycle-agent tricycle-agent run \
      rits.ssh.finetune configs/example-rits-ssh-finetune-request.toml \
      --run-id rits-internal-smoke --live

提交结果返回 external_id（远端 job_id）。状态、日志、停止、下载分别使用
rits.ssh.status、rits.ssh.logs、rits.ssh.terminate、rits.ssh.download。停止会调用
docker stop 并需 --confirm。下载请求只允许指定安全的相对 download_paths 和本地
download_dir，因此只取回 checkpoint/日志等所需产物，不复制整个数据 bundle。

## 长任务监控与恢复

提交成功只代表容器已启动，不代表训练已恢复或正在前进。首次检查应结合 status 与 logs：确认
运行时/GPU 校验通过、checkpoint 已成功恢复、日志中的 epoch/step 高于 checkpoint 保存值，且
train loss 为有限数值并持续更新。数据预处理期间可暂时没有 epoch 日志；不要仅因短暂无输出就
重启。监控中以 status 判断容器生命周期，以 logs 判断实际训练进展。

长任务使用已有监督流程或用户授权的监控执行者，避免多个进程同时管理同一 job。
监控者保留 job ID、请求文件和 run 目录，结合 status/logs 判断进展；若使用模型辅助监控，
模型与推理设置由用户或监督配置决定，不在技能中固定。

容器以非零状态退出时，先读取末尾 traceback 并确认状态为 exited，再决定是否重试。恢复优先使用
该 job `outputs/checkpoints/last.ckpt` 等最新 Lightning 完整 checkpoint，并验证其包含 `loops`、
`epoch`、`global_step`；仅含 `state_dict` 的模型 callback checkpoint 只能恢复权重，不能保证
Trainer 的 epoch/step 进度。若无可用完整 checkpoint，再使用用户提供或已验证的完整恢复点。每次
重试都使用新的 bundle/run/job ID，保留之前的 bundle、日志和 checkpoint；重试前不得有同一任务的
另一个训练容器仍在运行。新容器启动后重新核实 checkpoint 进度、`Restored all states` 和后续
epoch/loss 更新。不要因为重启而自行更改 batch size、epoch 数、优化器或训练数据。

对同一错误签名最多自动重试三次；反复失败、无可验证恢复点或疑似卡死时停止自动重试并向用户报告，
不得无限循环。停止仍在运行的远端容器必须遵守 `rits-ssh-terminate` 的显式确认要求；用户对重启的
授权不应被扩大为清理或删除旧任务目录。

## 任务管理请求

状态或日志请求的 TOML 示例：

    host = "gpu-box.internal"
    user = "trainer"
    remote_root = "/mnt/fast/rits-jobs"
    job_id = "rits-internal-smoke-generated-suffix"
    log_lines = 200

停止同样需要 host、remote_root、job_id，并执行：

    uv run --package tricycle-agent tricycle-agent run \
      rits.ssh.terminate request.toml --live --confirm

下载需要 download_paths = ["outputs"]（按实际训练输出配置选择）和 download_dir。避免
将含训练数据的大目录加入下载清单。
