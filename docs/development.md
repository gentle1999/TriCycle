# 开发说明

## 本地验证

```bash
make check
```

这会执行编译检查、标准库 smoke tests 和可用时的 Ruff 检查。安装 uv 后可运行完整依赖环境：

```bash
uv sync
uv run pytest
uv run ruff format --check .
uv run ruff check .
```

CI 使用 [`tools/ci_test.sh`](../tools/ci_test.sh) 运行锁定依赖、编译检查、Ruff、根目录
集成测试，以及 `packages/*/tests` 和 `apps/*/tests` 中每个 workspace 子项目的独立测试。
缺少 `tests/` 或 `test_*.py` 的子项目会使 CI 失败。

Bohr 的平台任务测试按代码边界放在具体 package 中：

- `packages/tricycle-bohr/tests/` 验证组件层的任务推送、任务状态查看和结果下载，以及默认 dry-run 不调用 gateway；
- `packages/tricycle-bohrsubmit/tests/` 验证新版 `bohr` CLI 的提交、查看、下载命令和参数映射；
- `packages/tricycle-rits/tests/` 验证 RitS 的 train/finetune/infer 请求、官方命令构造器、代码目录上传字段和公共 Bohr executor；
- `packages/tricycle-sar/tests/` 验证 RxnResid（兼容 `sar.*` 命名）对同一模型任务基础层的复用；
- 根目录 `tests/` 只保留跨 package 的注册表、框架契约和工作流 smoke test。

这些测试均使用内存 gateway 或 subprocess mock，不需要 Bohr 凭据，也不会创建远程任务。运行全部测试：

```bash
uv run pytest
make test
```

## 添加真实 adapter 的顺序

1. 先在对应组件中补充 protocol 所需的最小字段；
2. 用 fake/in-memory adapter 覆盖成功、重试、重复提交和失败状态；
3. 只在显式配置启用时读取环境变量和凭据；
4. 增加 integration test，并把网络/账号测试与默认 smoke test 分开；
5. 更新 `.agents/skills/<capability>/SKILL.md` 和 TOML 示例，说明输入 artifact、产物和幂等语义。
