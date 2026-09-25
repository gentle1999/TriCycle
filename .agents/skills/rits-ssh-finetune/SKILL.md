---
name: rits-ssh-finetune
description: "Stage and execute a RitS fine-tuning bundle in a Docker container on an internal GPU host over SSH."
---

# 内网 RitS Docker 微调

Bohr GPU 难以调度时使用 rits.ssh.finetune。协议职责分离为：本机通过 OpenSSH/scp 上传
任务 bundle，内网主机 Docker daemon 启动 GPU 容器；训练脚本不会在 SSH 主机裸机上运行。
容器以 /workspace/job（或 container_workdir）挂载远端任务目录，训练 stdout/stderr
由 Docker logging driver 保留并通过 rits.ssh.logs 读取。
执行命令使用非登录 Bash（`bash -c`）以保留镜像定义的 PATH 等环境变量；不要依赖登录 shell 初始化。

请求必须包含 host、remote_root、bundle_dir、command 和 container_image。GPU 默认
使用 container_gpus = "all"，也可使用 "0" 或 "0,1"。镜像必须在远端 Docker daemon
可访问的 registry 中，并匹配主机 NVIDIA driver / Container Toolkit；镜像需提供 Bash、
Python、RitS fork 依赖和兼容的 PyTorch/CUDA。提交前适配器会通过 Docker context 检查 docker info，但不猜测或
替换用户选定的镜像，也不隐式构建镜像。远端容器会设置 WANDB_MODE=disabled 和
WANDB_DISABLED=true。

远端 SSH 用户必须有权限访问 Docker daemon socket（通常属于 docker 组或使用 rootless
Docker）；本机 Docker CLI 通过 remote context 向远端 Docker API 发请求。

## 准备训练镜像

依赖镜像定义在 `packages/tricycle-rits/docker/cu130/`，基于远端已缓存的 Python 3.12
CUDA 12.8 基础镜像升级到官方 PyTorch 2.12.0+cu130，再安装 RitS fork 的 Python 与 PyG 依赖。
镜像只包含运行时和依赖，不包含 RitS 源码、训练数据或 checkpoint；训练时 bundle 会挂载到
`/workspace/job`，因此源代码更新无需重建镜像；如果依赖清单发生变化，则需重建。镜像环境变量 `RITS_DEPS_IN_IMAGE=1` 令 bundle
中的安装脚本跳过重复的依赖解析/下载，仍会执行运行时检查并将挂载源码以 editable 模式加载。

远端已有 `tricycle/base:torch-cu128-py312` 底座；构建会升级 Torch 并访问 PyTorch、PyPI、PyG
wheel index 下载依赖。显式构建并保存在远端 daemon：

    docker --context remote build \
      -t tricycle/rits-training:torch2.12.0-cu130 \
      packages/tricycle-rits/docker/cu130

build context 只有 Dockerfile 和依赖清单，不会把本地代码或数据送入镜像。构建后要核对 Python、
Torch/CUDA 版本和关键依赖导入成功；GPU 可用性要在运行容器时验证，因为 Docker build 阶段不挂载 GPU。

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
