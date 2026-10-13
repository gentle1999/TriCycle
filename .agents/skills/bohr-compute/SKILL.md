---
name: bohr-compute
description: "Plan and execute a computational chemistry task on the Bohr platform through an injected gateway."
---

# Bohr 计算

## 适用场景

当需要把反应配置、结构文件或模型推理任务转成 Bohr 计算任务时使用。先生成 `TaskPlan`，确认输入 artifact、镜像、资源和 operation，再决定是否提交。

## 输入

- `job_name`：一次任务的稳定名称；
- `operation`：如 `reaction.single-point`，由具体插件规格定义；
- `image`、`command`、可选 `project_id` 和 `machine_type`；
- `input_artifacts`、`output_artifacts` 和可序列化 `parameters`；
- 运行上下文中的 `run_id` 和 `dry_run`。

## 工作流

1. 用 `BohrComputeRequest` 校验请求。
2. 调用 `BohrComputePlugin.plan("compute", request, context)`。
3. 检查 `TaskPlan.parameters["job"]` 和输入 artifact 是否完整。
   DPA 计算必须申请 GPU 节点并使用 GPU 加速；纯 Gaussian 计算仅允许 CPU 节点。
4. 仅在用户/编排器明确允许且注入 `BohrGateway` 后调用 `submit`。
5. 保存外部 job id；后续通过 gateway 查询状态并下载产物。

## 输出与约束

- 规划阶段输出可 JSON 序列化的 `TaskPlan`；
- 提交阶段输出 `TaskResult(status=submitted, external_id=...)`；
- 默认 `dry_run=True`，不得读取凭据或访问网络；
- 不在该 skill 中猜测 Bohr 镜像内的命令、数据库字段或重试策略；这些内容属于 adapter 配置。

实现入口：`packages/tricycle-bohr/src/tricycle_bohr/plugin.py`。
