---
name: sar-finetune
description: "Plan or explicitly submit an RxnResid checkpoint-refit job to Bohr from a TOML request."
---

# RxnResid 微调（sar 兼容能力）

`sar.finetune` 对应上游 `https://github.com/gentle1999/RxnResid` 的
`train.py --refit --checkpoint <selection-checkpoint>`，不是普通训练中附带
`--checkpoint`。当前代码固定为 commit
`9deebde39b38df1085dbac3a379c2a59edd7c97d`，包含原生 evidential 不确定度 head。

`--refit` 从 selection checkpoint 读取 config、fold、split 和模型/encoder 设置，使用
train+valid 数据，以 checkpoint 权重和新的 optimizer 重新训练；可以指定
`--epochs`、`--epoch-factor`、`--learning-rate-factor`、`--batch-size`、`--device` 和
`--mixed-precision`。refit command 不得包含 `--config`、`--fold-index`、
`--split-manifest`、`--seed` 或模型/encoder override。

请求必须包含完整 RxnResid 代码目录、selection checkpoint artifact、`input_directory`、
worker 镜像和实际 command。refit 会从 checkpoint 恢复数据配置，因此不要求另传 dataset
artifact；如任务目录需要额外输入，使用 `input_artifacts` 声明。

推荐使用 `tricycle_sar.build_finetune_command`（别名
`build_refit_command`）生成 command：

```text
bash run.sh
```

`run.sh` should first execute `python3.12 -m pip install --no-cache-dir --editable .` and then run:

```text
python3.12 train.py \
  --refit --checkpoint runs/fold-00/checkpoint.pt \
  --output outputs/rxnresid-refit
```

要使用主动学习不确定度，selection checkpoint 必须来自包含该 head 的新版训练流程；
refit 后用新版 `predict.py` 生成 `aleatoric_variance`、`epistemic_variance`、
`predictive_variance`、对应标准差和 95% 区间，再交给 sampling skill。

先规划：

```bash
uv run --package tricycle-agent tricycle-agent plan \
  sar.finetune configs/example-rxnresid-finetune-request.toml --run-id rxnresid-finetune-smoke
```

真实提交必须显式使用 `run ... --live`。默认 dry-run 不读凭据、不联网、不上传、不提交
Bohr；已提交任务使用 `bohr.job.status` 和 `bohr.job.download`。
