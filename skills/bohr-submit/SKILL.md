---
name: bohr-submit
description: "Prepare and operate typed Bohr batch, Gaussian, and DPA-Berny workflows through an injected gateway."
---

# Bohr 批量与化学计算工作流

## 当前 Bohr CLI

本插件按当前 `bohr` CLI 2.x 接口调用：生命周期操作使用 `bohr` 返回的公开
`BOHR_ID`（JSON 字段 `bohrId`），机读结果使用 `--output json`，下载/日志使用 `--out`。
新版输出中的平台内部 `id` 不能直接用于生命周期操作；带有 `job_group_id` 的旧批次由
adapter 通过 `job describe --job_group_id` 自动映射。旧版 `-j` 任务 ID、`-o` 本地输出
路径和 `--dataset_path` 提交参数不能直接复制到新版命令。

当前 CLI 没有 `--backward_files` flag，但 `--config_file` 的 JSON schema 支持
`backward_files: ["relative/path", "glob/**"]`，提交后对应 API 的 `outFiles`。gateway
会在任务创建时为 `output_artifacts` 或显式 `parameters.backward_files` 生成只含该白名单的
临时 JSON 配置，并在提交完成后删除；未声明任何结果路径时保留 Bohr 默认的全量回传行为。
`result_path` 仅表示自动结果下载到 Bohrium `/personal` 或 `/share` 云盘的目标目录，不是
结果白名单。临时配置不含凭据。

认证由进程环境提供。工具会自动发现当前工作目录或父目录中的 `.env`；新版优先使用
`BOHR_ACCESS_KEY`，为兼容旧部署，adapter 会把已有的 `ACCESS_KEY` 仅映射到 Bohr 子进程：

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

- 通用批量：`tasks`、`workers`、`script`（或 `worker_script`）、镜像、项目和机型；
- DPA-Berny/Gaussian：`xyz`、`workers`、`engine`、Gaussian 参数，以及 DPA 模型（仅 `engine=dpa`）；
- 远程生命周期：已生成的 `run_dir` 和显式注入的 `BohrWorkflowGateway`；
- 默认镜像：`registry.dp.tech/dptech/dp/native/prod-23278/gaussian-orca-dp-client:260824`。

## 工作流

1. 先调用 `BohrSubmitPlugin.plan("batch.prepare", ...)` 或
   `plan("dpa-berny.prepare", ...)` 检查请求，再调用 `prepare` 生成本地运行目录。
2. 检查 `batch.toml`、`tasks.tsv`、`submit-plan.tsv` 和 worker 输入；准备阶段不连接 Bohr。
3. 只有显式注入非 dry-run gateway 后调用 `submit_prepared`。
4. 用 `status`、`download` 和 `watch` 维护运行；长尾计算先下载完整 worker 结果，再调用 `rescue`。
5. 约束 xyz 会生成 fixed-opt + Link1；`multi_stage_chk` 会生成编号 checkpoint。

## ORCA 约束

旧项目没有定义 ORCA 输入文件格式，因此本插件不猜测 ORCA route 或输入模板。使用通用 `batch.prepare`，通过显式 `script` 调用镜像内的 `orca`；待 ORCA 任务 schema 明确后再增加专用 planner。

## 输出与禁止事项

- 输出为可审计的 `TaskPlan`、运行目录和 TSV 生命周期表；
- 声明式请求和运行配置使用 TOML；worker 使用的 `dpa-berny.env` 是 Bash
  进程环境桥接，不是配置文件；
- 默认 `dry_run=True`，不得读取凭据、访问网络或提交远程任务；只有 live gateway
  构造时才加载 `.env` 中的认证信息；
- gateway 是唯一的 `bohr` CLI 边界；不在插件中直接调用 subprocess；
- 不把 token 写入 TOML、环境投影、计划或日志。

实现入口：`packages/tricycle-bohrsubmit/src/tricycle_bohrsubmit/`。
