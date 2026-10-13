# 余额邮件告警

`tools/balance_alert.py` 是独立确定性监控器，不调用模型、不暂停或重提实验、不充值。
余额读取与邮件发送通过 `BalanceSource` / `MailSender` 注入。
提供显式命令 JSON adapter 和 TLS SMTP adapter。

## 配置

复制 `configs/example-balance-alert.toml`，确认实际账户、单位、阈值及收件人。
示例数值和邮箱不是已启用配置。`command` 为不经过 shell 的 argv 数组；
`json_path` 为提供方余额字段路径，`multiplier` 用于元/分等明确单位转换。
查询必须只读，30 秒内返回 JSON。不能把 token 限额或认证状态当作余额。

Bohrium CLI 提供 `bohr billing balance` / `bohr billing photon`；必须确认实际
消费来源、返回字段及单位，不能混加现金和光子。模型余额由实际计费提供方查询。
SMTP host、username、password 通过指定环境变量注入，不写入配置或审计记录。
支持 STARTTLS（通常 587）或 SSL（通常 465）。

## 运行

```bash
uv run --frozen --all-packages python -m tools.balance_alert tick --config configs/balance-alert.toml
uv run --frozen --all-packages python -m tools.balance_alert watch --config configs/balance-alert.toml --run-dir /absolute/monitor --live
```

默认 dry-run 不读凭据、不查询、不发邮件。真实运行必须 `--live`。
首次查询已低于阈值时就会发送邮件。默认每 5 分钟查询：

- 余额不高于阈值触发低余额告警；不高于零升级为耗尽告警。
- 同状态成功发送后默认 12 小时再次提醒。
- 恢复到阈值之上后重置去重，再次跌破重新告警。
- 查询连续失败 3 次通知“余额监控不可用”，不冒充余额为零。
- 邮件失败 15 分钟后重试，仅记录错误类型，不保存接口原始错误或凭据。

SMTP 接受邮件后断线可能导致后续重复发送，不能承诺 exactly-once。
`state.json` 记录余额、查询时间和邮件状态；`notifications/*.json` 记录发送回执。
文件锁防止同目录多进程重复告警。

生成 macOS 服务模板：

```bash
uv run --frozen --all-packages python -m tools.balance_alert service-file --config configs/balance-alert.toml --run-dir /absolute/monitor --live
```

只生成 plist，不安装、不发送。通过服务管理器提供 SMTP 环境并安装 LaunchAgent。
服务环境与终端不同，必须确认余额命令认证和 SMTP 环境可用；模板不复制密钥。

## Codex 使用额度 adapter

通过官方 Codex App Server 的 account/rateLimits/read 读取当前 ChatGPT 登录账户的
codex bucket；不启动 thread/turn，不调用模型，不读取或打印 token。
来源：https://learn.chatgpt.com/docs/app-server （Rate limits 部分）。

`python -m tools.codex_quota --live` 返回各 primary/secondary 窗口剩余比例、重置时间，
amount 为窗口剩余比例最小值。默认不加 --live 不访问账户。
缺少额度窗口时报告查询失败，不用 credits 或 API 余额冒充。
额度属于当前 Codex 登录账户；切换登录会切换监控对象。

部署方应复制示例配置到本机私有配置文件，设置阈值、只读查询命令和收件人；
完成 SMTP 配置前不要启用 live 发送。百分比来自官方 usedPercent，计算为
max(0, 100-usedPercent)，不估算美元。
如提供方返回剩余额度与总额度，也可使用 remaining_percent 模式和 total_json_path；
分母必须明确且大于零，不默认以历史峰值或最近充值额为总额。

## 本地部署配置

将 live 配置、收件地址、服务器地址和本机文件路径保存在 Git 忽略的私有配置中；
口令应由环境变量或权限受限的本机文件提供，不纳入仓库。SMTP 支持显式 `env_file`，
只加载配置指定的 host、username、password 键；进程环境优先。只在 live 的配置检查和
发送阶段读取，不将凭据传给额度查询子进程或写入 plist。`mail-status.json` 只表示本地
凭据配置状态，不代表成功认证或成功投递。
