---
name: rits-ssh-logs
description: "Read the tail of a remote RitS Docker training log over SSH."
---

# Read Internal RitS Logs

Use `rits.ssh.logs` with `host`, `remote_root`, and `job_id` in TOML. `log_lines` defaults to 200 and is capped at 5000. Only explicit `--live` reads container logs through the SSH-backed Docker context. See [RitS SSH fine-tuning](../rits-ssh-finetune/SKILL.md).
