# Email balance alerts

`tools/balance_alert.py` is a standalone deterministic monitor. It does not call models, pause or resubmit experiments, or add funds. Balance reads and email delivery are injected through `BalanceSource` / `MailSender`, with explicit command-JSON and TLS SMTP adapters.

SMTP delivery is implemented once in `tricycle_core.mail` and shared with
[trajectory alerts](trajectory-alerts.md). Existing balance-monitor configuration
remains supported; standard `TRICYCLE_SMTP_*` environment settings override its
corresponding routing fields. New trajectory-alert routing is entirely environment
configured. Constructing a sender does not load credentials or contact SMTP.

New monitor state defaults to `.tricycle/monitors/<date>/<timestamp>/balance-alert/`, outside experiment runs. An explicit `--run-dir` remains supported, including existing deployed services. Follow [run record scope](run-records.md); do not move a running monitor's state or synchronization files as part of directory cleanup.

## Configuration

Copy `configs/example-balance-alert.toml` and verify the actual account, units, thresholds, and recipients. Example values and email addresses are not enabled settings. `command` is an argv array executed without a shell. `json_path` locates the provider's balance field; `multiplier` performs an explicit unit conversion such as dollars to cents. Queries must be read-only and return JSON within 30 seconds. Token limits or authentication status are not balances.

Bohrium CLI provides `bohr billing balance` / `bohr billing photon`. Verify the actual billing source, returned fields, and units; never add cash and photons together. Model balances must come from the actual billing provider. Inject SMTP host, username, and password through the specified environment variables, outside configuration and audit records. STARTTLS (usually port 587) and SSL (usually port 465) are supported.

## Execution

```bash
uv run --frozen --all-packages python -m tools.balance_alert tick --config configs/balance-alert.toml
uv run --frozen --all-packages python -m tools.balance_alert watch --config configs/balance-alert.toml --run-dir /absolute/monitor --live
```

Default dry runs do not read credentials, query balances, or send email. Live operation requires `--live`. A first reading below the threshold triggers an email. The default polling interval is five minutes:

- A balance at or below the threshold triggers a low-balance alert; a balance at or below zero escalates to exhaustion.
- After successful delivery, unchanged states are reminded after 12 hours by default.
- Recovery above the threshold resets deduplication; a later fall triggers another alert.
- Three consecutive query failures trigger a balance-monitor-unavailable notice rather than reporting a zero balance.
- Email failures retry after 15 minutes. Only error types are recorded, without raw API errors or credentials.

A disconnect after SMTP acceptance can cause duplicate delivery; exactly-once delivery is not guaranteed. `state.json` records balance, query time, and mail state; `notifications/*.json` stores delivery receipts. A file lock prevents multiple processes in the same directory from sending duplicate alerts.

Generate a macOS service template:

```bash
uv run --frozen --all-packages python -m tools.balance_alert service-file --config configs/balance-alert.toml --run-dir /absolute/monitor --live
```

This only generates a plist; it neither installs a service nor sends email. Provide SMTP environment variables through the service manager and install the LaunchAgent separately. Service environments differ from terminals: verify balance-command authentication and SMTP settings. Templates do not copy secrets.

## Codex quota adapter

The adapter reads the current ChatGPT-authenticated account's `codex` bucket through the official Codex App Server `account/rateLimits/read` method. It does not start threads/turns, call models, or read/print tokens. The documented source is the rate-limits section of the [App Server documentation](https://learn.chatgpt.com/docs/app-server).

`python -m tools.codex_quota --live` returns remaining percentages and reset times for primary/secondary windows; `amount` is the minimum remaining percentage across windows. Without `--live`, it does not access the account. Missing quota windows cause a query failure; credits or API balances are not substitutes. Quota belongs to the current Codex login, so changing the login changes the monitored account.

Deployments should copy the example into a private local configuration and set thresholds, the read-only query command, and recipients. Do not enable live delivery until SMTP is configured. Percentages come from the official `usedPercent`, calculated as `max(0, 100-usedPercent)` without estimating dollars. If a provider supplies remaining and total quotas, `remaining_percent` mode and `total_json_path` are also available. The denominator must be explicit and positive, not an assumed historical peak or recent top-up amount.

## Local deployment configuration

Store live settings, recipient addresses, server addresses, and local paths in Git-ignored private configuration. Passwords belong in environment variables or permission-restricted local files. SMTP supports an explicit `env_file`, loading only the configured host, username, and password keys; process environment values take precedence. It is read only during live configuration checks and sending. Credentials are neither passed to quota-query subprocesses nor written into plist files. `mail-status.json` reports local credential-configuration status, not successful authentication or delivery.
