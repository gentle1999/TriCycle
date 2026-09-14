# 变更日志

## 2026-09-14

### 修复

- `bohr.batch.download` 不再根据 worker 整体的 `FAILED` 状态跳过下载；失败 worker 中的部分成功任务、日志和诊断结果也会被尝试拉取，并逐 worker 记录下载结果。

### 新增

- 在 `tricycle-reaction-db` 中实现 `McpReactionDatabaseAdapter`，通过数据库 Streamable HTTP
  MCP 的 project、manifest、import-job 和 compose-query 工具完成导入、筛选与导出；MCP 导入
  使用数据库服务可见的暂存路径和文件哈希，不通过 MCP 传输日志字节。
- live agent 自动注入 MCP adapter；数据库 MCP URL、token、organization ID、暂存根目录和
  TLS/超时策略只从环境变量读取，dry-run 不访问网络。

## 2026-09-13

### 新增

- 增加基于 JSONL、哈希链和文件锁的运行轨迹记录，支持对任意快照进行完整性校验。
- 增加可复现探索轮次模块，记录完整候选池、模型分数、采样排序与排除理由、下一轮空间范围决策、RitS train/skip 决策以及快照迁移。
- 为 train、finetune 和 skip 迁移生成稳定的 replay key，使相同快照配合相同轮次决策可以复现下一个快照（QM 计算按相同输入可重复处理）。
- 增加 CLI run/resume 的请求、计划、能力执行和 trace 验证记录，并支持探索轮次安全复用。
- 增加 predictive、epistemic、aleatoric uncertainty 采样策略及候选分数的确定性记录。
- 将 RxnResid 默认源码锁定到 `9deebde39b38df1085dbac3a379c2a59edd7c97d`，记录原生 evidential
  不确定度输出，并让 sampling 直接消费 `*_variance`/`*_std` 字段；`epistemic_variance` 可用于主动学习。

### 验证

- `make check`
- `uv run pytest`（100 passed）
- `uv lock --check`
- `ruff check .`
- `git diff --check`
