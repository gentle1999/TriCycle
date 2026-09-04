# TriCycle Agent 开发指南

## 工程约定

- Python `>=3.11`，每个可发布子项目使用 `src/` 布局。
- 包管理使用 `uv` workspace；根目录的 `Makefile` 提供不依赖 uv 的基础 smoke test。
- 公共跨插件类型放在 `packages/tricycle-core`，不要让 core 反向依赖具体插件。
- 外部服务必须通过 protocol/adapter 注入。不要在 dry-run 默认路径中读取凭据、发起网络请求或提交真实任务。
- 插件的智能体操作说明放在 `skills/<skill-name>/SKILL.md`，代码接口与 skill 必须同步更新。

## 常用命令

```bash
make check
make test
make list-plugins
uv sync
uv run pytest
uv run ruff check .
```

## 新增插件的最小要求

1. 在 `packages/` 下创建独立子项目和 `src/<package_name>/`。
2. 实现 `PluginBase` 的 manifest 和 `plan`，输入请求使用明确的 dataclass。
3. 为外部副作用定义 protocol，并提供 dry-run 或内存 adapter。
4. 在 `apps/tricycle-agent/src/tricycle_agent/bootstrap.py` 注册插件。
5. 添加对应 `skills/<name>/SKILL.md`、示例配置和 smoke test。

具体边界见 `docs/plugin-contract.md`。业务规则不明确时保留 TODO 和扩展点，不猜测真实平台 API。

