---
name: rits-ssh-terminate
description: "Stop a remote RitS Docker training container after explicit confirmation."
---

# Stop Internal RitS Training

`rits.ssh.terminate` calls Docker stop with a maximum 30-second wait. This interrupts training and requires explicit `--live --confirm`. Supply `host`, `remote_root`, and `job_id`, then verify exit with `rits.ssh.status`. See [RitS SSH fine-tuning](../rits-ssh-finetune/SKILL.md).
