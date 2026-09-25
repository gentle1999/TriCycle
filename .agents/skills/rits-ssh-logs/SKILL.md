---
name: rits-ssh-logs
description: "Read the tail of a remote RitS Docker training log over SSH."
---

# 查询内网 RitS 日志

使用 rits.ssh.logs 并在 TOML 请求中提供 host、remote_root、job_id；log_lines
默认 200、最大 5000。显式 --live 后才通过 SSH-backed Docker context 读取容器 logs。详见
[RitS SSH 微调](../rits-ssh-finetune/SKILL.md)。
