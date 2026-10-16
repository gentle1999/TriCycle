---
name: rits-ssh-download
description: "Download explicitly selected outputs from a remote RitS Docker training job."
---

# Download Internal RitS Outputs

Use `rits.ssh.download` with `host`, `remote_root`, `job_id`, safe relative `download_paths`, and local `download_dir`. Download via scp occurs only with explicit `--live`. Select needed checkpoints/logs, not the entire remote code/data directory. See [RitS SSH fine-tuning](../rits-ssh-finetune/SKILL.md).

Environment/cache selectors are rejected. Downloads first enter disposable storage;
loose files and ZIP/tar members are checked before publication. Contamination inside
a selected directory is detected after transfer, so narrow the remote selection
when validation fails. Existing local evidence is preserved, identical files are
reused, and conflicting files fail instead of being overwritten.
