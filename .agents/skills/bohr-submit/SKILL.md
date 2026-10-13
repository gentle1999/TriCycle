---
name: bohr-submit
description: "Prepare and operate typed Bohr batch, Gaussian, and DPA-Berny workflows through an injected gateway."
---

# Bohr 批量与化学计算工作流

## 当前 Bohr CLI

本插件按当前 `bohr` CLI 2.x 接口调用：生命周期操作使用 `bohr` 返回的公开
`BOHR_ID`（JSON 字段 `bohrId`），机读结果使用 `--output json`，下载/日志使用 `--out`。
平台内部 `id` 不能用于生命周期操作；请求必须提供公开 `BOHR_ID`。含有
`bohr-id-map.tsv` 的迁移运行目录会被拒绝，应使用公开 ID 创建新请求；adapter 不自动映射内部 ID。

当前 CLI 没有 `--backward_files` flag，但 `--config_file` 的 JSON schema 支持
`backward_files: ["relative/path", "glob/**"]`，提交后对应 API 的 `outFiles`。gateway
会在任务创建时为 `output_artifacts` 或显式 `parameters.backward_files` 生成只含该白名单的
临时 JSON 配置，并在提交完成后删除；未声明任何结果路径时保留 Bohr 默认的全量回传行为。
`result_path` 仅表示自动结果下载到 Bohrium `/personal` 或 `/share` 云盘的目标目录，不是
结果白名单。临时配置不含凭据。

认证使用进程环境中的 `BOHR_ACCESS_KEY`。工具会自动发现当前工作目录或父目录中的 `.env`：

```bash
export BOHR_ACCESS_KEY='...'
bohr auth status --verify
```

请求 TOML 未填写时，`BOHR_PROJECT_ID`、`BOHR_MACHINE_TYPE` 和 `BOHR_IMAGE`
会提供提交默认值；它们不是认证凭据。

如果需要使用远程数据集，应先把数据放入 `input_directory`，或注入支持这些能力的专用
gateway；当前 CLI gateway 会对无法表示的 dataset path 明确报错。结果文件选择应通过
`output_artifacts` 或 `parameters.backward_files` 声明。

## 适用场景

当需要把一批任务文件或 xyz 结构准备成 Bohr worker 目录，或需要管理已准备运行的提交、状态、下载、监控和救援时使用。

## 输入

CLI adapter 策略 TOML 使用 `[bohr]` 与 `[retry]` 表，字段见
[配置示例](../../../configs/example-bohr.toml)。省略项使用默认值；平铺字段、未知表或未知字段均报错。

- 通用批量：`task_patterns`、`workers`、`script`（或 `worker_script`）、镜像、项目和机型；
- DPA-Berny/Gaussian：`xyz_patterns`、`workers`、`engine`、Gaussian 参数，以及 DPA 的 `model_path`（仅 `engine=dpa`）；
- DPA 必须选择 GPU `machine_type` 并使用 GPU 加速；纯 Gaussian 只允许 CPU 节点。
  准备、提交及救援均校验资源，DPA 不允许回退 CPU；验收需包含 GPU 设备与实际计算证据；
- 远程生命周期：已生成的 `run_dir` 和显式注入的 `BohrWorkflowGateway`；
- 镜像、项目与机型读取请求及 adapter 支持的环境默认值，检查最终计划，不从技能复制固定镜像标签。

## 工作流

1. 先调用 `BohrSubmitPlugin.plan("batch.prepare", ...)` 或
   `plan("dpa-berny.prepare", ...)` 检查请求，再调用 `prepare` 生成本地运行目录。
2. 检查 `batch.toml`、`tasks.tsv`、`submit-plan.tsv` 和 worker 输入；准备阶段不连接 Bohr。
3. 只有显式注入非 dry-run gateway 后调用 `submit_prepared`。
4. 用 `status`、`download` 和 `watch` 维护运行；长尾计算先下载完整 worker 结果，再调用 `rescue`。
5. 约束 xyz 会生成 fixed-opt + Link1；`multi_stage_chk` 会生成编号 checkpoint。

CLI 对已有运行使用 `inspect` 后的 `resume`，它执行 `resume.next_action`，并非固定查询或下载命令。只有下一动作与本次意图相符时才恢复；已有 `run.toml` 不能再用 `run` 初始化。需要独立指定生命周期动作时调用注入 gateway 的包内 API。下载报告在 `downloads/download-results.tsv`。

## ORCA 约束

本插件尚未定义 ORCA 输入文件格式，不猜测 ORCA route 或输入模板。使用通用 `batch.prepare`，通过显式 `script` 调用镜像内的 `orca`；待 ORCA 任务 schema 明确后再增加专用 planner。

## 输出与禁止事项

- 输出为可审计的 `TaskPlan`、运行目录和 TSV 生命周期表；
- 声明式请求和运行配置使用 TOML；worker 使用的 `dpa-berny.env` 是 Bash
  进程环境桥接，不是配置文件；
- 默认 `dry_run=True`，不得读取凭据、访问网络或提交远程任务；只有 live gateway
  构造时才加载 `.env` 中的认证信息；
- gateway 是唯一的 `bohr` CLI 边界；不在插件中直接调用 subprocess；
- 不把 token 写入 TOML、环境投影、计划或日志。

实现入口：`packages/tricycle-bohrsubmit/src/tricycle_bohrsubmit/`。

同参数的多折/多seed任务优先在一份请求中使用 `task_defaults` + `task_specs`，与 `task_patterns` 互斥。任务行只写唯一 `name` 及差异字段，不覆盖公共参数。staging自动展开为worker输入并均衡分配，输入与协议哈希检查照常执行。
