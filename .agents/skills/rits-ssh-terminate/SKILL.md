---
name: rits-ssh-terminate
description: "Stop a remote RitS Docker training container after explicit confirmation."
---

# 停止内网 RitS 任务

使用 rits.ssh.terminate 调用 Docker stop（最长等待 30 秒）。此操作会中断训练，必须显式
使用 --live --confirm；请求中提供 host、remote_root、job_id。随后可用 rits.ssh.status 核对
容器已退出。详见 [RitS SSH 微调](../rits-ssh-finetune/SKILL.md)。
