---
name: rits-ssh-status
description: "Inspect the status of a detached RitS Docker training job on an internal GPU host."
---

# 查询内网 RitS 任务

使用 rits.ssh.status 查询远端 Docker 容器状态。请求 TOML 至少提供
host、remote_root、job_id；SSH 认证使用本机 OpenSSH agent/config。
该能力只规划时不连接，只有显式 --live 才会发起 SSH 只读查询。详见
[RitS SSH 微调](../rits-ssh-finetune/SKILL.md)。
