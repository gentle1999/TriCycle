---
name: rits-ssh-download
description: "Download explicitly selected outputs from a remote RitS Docker training job."
---

# 下载内网 RitS 产物

使用 rits.ssh.download，请求中指定 host、remote_root、job_id、相对安全路径列表
download_paths 和本地 download_dir。仅显式 --live 时经 scp 下载。只选择 checkpoint、
日志等需要的输出路径，不要下载含数据集与代码的整个远端任务目录。详见
[RitS SSH 微调](../rits-ssh-finetune/SKILL.md)。
