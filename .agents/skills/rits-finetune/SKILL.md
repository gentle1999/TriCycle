---
name: rits-finetune
description: "Plan or explicitly submit a RitS fine-tuning job to Bohr from a TOML request."
---

# RitS 微调

RitS 微调使用上游 `scripts/train.py` 的 Hydra override
`finetune_from=<checkpoint>`（`init_from_ckpt` 是兼容别名）。它只加载 checkpoint
权重，并以当前配置创建新的 optimizer、scheduler 和训练运行；不要把它和断点续训
`resume=<checkpoint-or-run-directory>` 混为一谈。

当前代码必须来自 `https://github.com/gentle1999/RitS` 的 commit
`6d8aa0704c21d2798326a7e070491abf438ca292`。请求必须包含代码目录、RitS 数据集
artifact、checkpoint artifact、`input_directory`、worker 镜像和实际 command。数据集
根目录、输出目录、epoch 等保持为 RitS 原生 Hydra 配置或 override。

推荐使用 `tricycle_rits.build_finetune_command` 生成 command，形如：

```text
bash run.sh
```

`run.sh` should first execute `python3.12 -m pip install --no-cache-dir --editable .` and then run:

```text
python3.12 scripts/train.py \
  --config-path=conf --config-name=rits \
  data.dataset_root=./data/rits_dataset \
  finetune_from=./data/rits.ckpt
```

先规划并确认 artifact、`source_repository`、`source_revision` 和 command：

```bash
uv run --package tricycle-agent tricycle-agent plan \
  rits.finetune configs/example-rits-finetune-request.toml --run-id rits-finetune-smoke
```

真实提交必须显式使用 `run ... --live`。默认 dry-run 不读凭据、不联网、不上传、不提交
Bohr；已提交任务使用 `bohr.job.status` 和 `bohr.job.download`，不要重复创建任务。
