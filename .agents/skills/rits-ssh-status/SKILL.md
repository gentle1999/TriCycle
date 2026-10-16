---
name: rits-ssh-status
description: "Inspect the status of a detached RitS Docker training job on an internal GPU host."
---

# Query Internal RitS Status

Use `rits.ssh.status` for remote Docker container state. TOML requires `host`, `remote_root`, and `job_id`; authentication uses local OpenSSH agent/config. Planning does not connect. Only explicit `--live` performs the read-only SSH query. See [RitS SSH fine-tuning](../rits-ssh-finetune/SKILL.md).
