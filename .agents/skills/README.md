# TriCycle 项目技能

`.agents/skills/` 是项目唯一技能目录，统一保存能力操作、领域工作流和开发规范。
每个名称只有一份 `SKILL.md`；不维护平行目录、同名副本或只做转发的技能。
模块接口见所属包 README；具体版本、hash 与镜像绑定读取配置或资产清单，不复制进技能。

## 能力操作

这些技能对应应用能力目录中的实际 capability。默认规划不连接外部服务；操作的授权与恢复边界见各技能。

| Skill | Capability |
| --- | --- |
| [reactionopt-generate](reactionopt-generate/SKILL.md) | `reactionopt.generate` |
| [reactionopt-enumerate-da](reactionopt-enumerate-da/SKILL.md) | `reactionopt.enumerate-da` |
| [reactionopt-export](reactionopt-export/SKILL.md) | `reactionopt.export` |
| [reactionopt-profile](reactionopt-profile/SKILL.md) | `reactionopt.profile` |
| [candidate-select](candidate-select/SKILL.md) | `candidate.select` |
| [exploration-round](exploration-round/SKILL.md) | `exploration.round` |
| [bohr-compute-plan](bohr-compute-plan/SKILL.md) | `bohr.compute.plan` |
| [bohr-compute-submit](bohr-compute-submit/SKILL.md) | `bohr.compute.submit` |
| [bohr-job-status](bohr-job-status/SKILL.md) | `bohr.job.status` |
| [bohr-job-download](bohr-job-download/SKILL.md) | `bohr.job.download` |
| [bohr-batch-prepare](bohr-batch-prepare/SKILL.md) | `bohr.batch.prepare` |
| [bohr-batch-submit](bohr-batch-submit/SKILL.md) | `bohr.batch.submit` |
| [bohr-batch-status](bohr-batch-status/SKILL.md) | `bohr.batch.status` |
| [bohr-batch-download](bohr-batch-download/SKILL.md) | `bohr.batch.download` |
| [bohr-batch-watch](bohr-batch-watch/SKILL.md) | `bohr.batch.watch` |
| [bohr-batch-terminate](bohr-batch-terminate/SKILL.md) | `bohr.batch.terminate` |
| [dpa-berny-prepare](dpa-berny-prepare/SKILL.md) | `dpa-berny.prepare` |
| [dpa-berny-rescue](dpa-berny-rescue/SKILL.md) | `dpa-berny.rescue` |
| [reaction-db-ingest](reaction-db-ingest/SKILL.md) | `reaction-db.ingest` |
| [reaction-db-batch-import](reaction-db-batch-import/SKILL.md) | `reaction-db.batch-import` |
| [reaction-db-filter](reaction-db-filter/SKILL.md) | `reaction-db.filter` |
| [reaction-db-export-rits](reaction-db-export-rits/SKILL.md) | `reaction-db.export-rits` |
| [reaction-db-export-rxnresid](reaction-db-export-rxnresid/SKILL.md) | `reaction-db.export-rxnresid` |
| [sar-train](sar-train/SKILL.md) | `sar.train` |
| [sar-infer](sar-infer/SKILL.md) | `sar.infer` |
| [sar-finetune](sar-finetune/SKILL.md) | `sar.finetune` |
| [rits-train](rits-train/SKILL.md) | `rits.train` |
| [rits-infer](rits-infer/SKILL.md) | `rits.infer` |
| [rits-finetune](rits-finetune/SKILL.md) | `rits.finetune` |
| [rits-ssh-finetune](rits-ssh-finetune/SKILL.md) | `rits.ssh.finetune` |
| [rits-ssh-status](rits-ssh-status/SKILL.md) | `rits.ssh.status` |
| [rits-ssh-logs](rits-ssh-logs/SKILL.md) | `rits.ssh.logs` |
| [rits-ssh-terminate](rits-ssh-terminate/SKILL.md) | `rits.ssh.terminate` |
| [rits-ssh-download](rits-ssh-download/SKILL.md) | `rits.ssh.download` |

## 领域与组合工作流

| Skill | 用途 |
| --- | --- |
| [bohr-compute](bohr-compute/SKILL.md) | 单任务计算的规划、提交与结果生命周期 |
| [bohr-submit](bohr-submit/SKILL.md) | 批量 staging、计算输入与生命周期 |
| [reactionopt-config](reactionopt-config/SKILL.md) | 反应配置生成与下游交接 |
| [rits-model](rits-model/SKILL.md) | RitS 训练、微调、推理及执行器选择 |
| [sar-model](sar-model/SKILL.md) | SAR 后端、任务准备与结果管理 |
| [sar-benchmark](sar-benchmark/SKILL.md) | 分组交叉验证、多模型比较与发布 |
| [sampling-strategy](sampling-strategy/SKILL.md) | 策略选择、匹配实验与采样边界 |
| [agent-authored-acquisition](agent-authored-acquisition/SKILL.md) | 按所选协议编写并核验逐轮采集函数 |
| [exploration-campaign](exploration-campaign/SKILL.md) | 持久 QM 探索或历史标签回放的监督与恢复 |

## 开发规范

| Skill | 用途 |
| --- | --- |
| [code-structure](code-structure/SKILL.md) | 目录职责、依赖方向、配置归属与迁移验证 |
| [commit-log](commit-log/SKILL.md) | 英文 Conventional Commits 提交说明与提交整理 |

能力请求和 adapter 策略使用 TOML；模型原生输入、replay 状态与结果文件遵循所属模块的数据契约。命令输出不能直接当作请求配置。
