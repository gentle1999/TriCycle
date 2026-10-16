# Blocked trajectory email alerts

Replay supervisors can send an email when a seed's recorded `phase` becomes
`blocked`. A reporting-only subagent writes equivalent English and Chinese subjects
and bodies from compact failure evidence. Its instructions and operational records
remain English; the email fields are the user's explicit bilingual exception.
The subagent cannot select recipients, send mail, resume work, or alter a protocol.

The shared `tricycle_core.mail.SmtpSender` handles delivery. The existing balance
monitor also imports this implementation rather than maintaining another SMTP
client. Notifications are disabled by default.

## Configuration

Copy `configs/example-trajectory-alerts.toml` to the Git-ignored
`configs/trajectory-alerts.toml`. It configures the reporting agent, timeouts,
bounded retries, polling, and local notification storage. Relative paths are
resolved against the supervisor's working directory. SMTP routing and credentials
are not TOML fields for this feature; set them through the process environment or
the project-local, Git-ignored `.env`. Process environment values take precedence.

| Variable | Meaning |
| --- | --- |
| `TRICYCLE_SMTP_HOST` | SMTP server hostname |
| `TRICYCLE_SMTP_PORT` | SMTP port; defaults to 587 for STARTTLS or 465 for SSL |
| `TRICYCLE_SMTP_TLS` | `starttls` or `ssl`; certificates and hostnames are verified |
| `TRICYCLE_SMTP_USERNAME` | SMTP login, when required by the provider |
| `TRICYCLE_SMTP_PASSWORD` | SMTP password or provider-issued app password; configure together with username |
| `TRICYCLE_SMTP_SENDER` | One plain sender email address |
| `TRICYCLE_SMTP_RECIPIENTS` | Comma-separated plain recipient email addresses |
| `TRICYCLE_SMTP_TIMEOUT_SECONDS` | SMTP operation timeout, default 30 seconds |
| `TRICYCLE_TRAJECTORY_ALERTS_ENABLED` | Explicit `true` to enable live notifications; default `false` |
| `TRICYCLE_TRAJECTORY_ALERT_CONFIG` | Notification TOML path, default `configs/trajectory-alerts.toml` |

`.env.example` lists all variables without real credentials or addresses. The
reporting agent has its own explicit command, model, and reasoning settings; these
do not change the sampling decision agent. It uses the existing Codex command
adapter convention and must be installed and authenticated separately. SMTP
variables are removed from the author process environment; prompts contain neither
routing information nor SMTP credentials.

## Operation

Normal live campaign/suite watchers and the seed recovery watcher invoke alerts
after a blocked state is recorded. Pending notification retries keep that blocked
seed's watcher alive within the configured attempt budget; other seed threads
continue independently. A one-tick invocation makes at most one delivery attempt.
Paused, running, and completed states do not trigger a blocked-state alert.

An independent observer can inspect existing records without advancing scientific
work or changing frozen controller hashes:

```bash
# Dry-run: inspect blocked states and validate the explicit TOML, without loading
# mail credentials, calling an agent, sending mail, or writing notification state.
uv run --all-packages python -m tricycle_sampling.replay.alerts scan \
  --run-dir /absolute/suite-or-experiment \
  --config configs/example-trajectory-alerts.toml

# Live observer: requires the enable environment variable and configured SMTP.
uv run --all-packages python -m tricycle_sampling.replay.alerts watch \
  --run-dir /absolute/suite-or-experiment --live

# Explicitly grant another bounded retry cycle after fixing author/SMTP failures.
uv run --all-packages python -m tricycle_sampling.replay.alerts scan \
  --run-dir /absolute/suite-or-experiment --live --retry-failed
```

The observer accepts a suite, experiment, or individual seed directory. It reads
unpacked `seed-*/state.json` records only; it does not unpack archives. It does not
infer deadlocks from elapsed time or detect a supervisor killed before it records
`blocked`. Use the host service manager for process liveness monitoring.

For an existing source-bound experiment, do not rewrite frozen hashes just to
enable alerts. Use the independent observer until an explicitly audited controller
update permits the new integrated watcher. Generated campaign/suite service files
may carry the two non-secret alert-control variables; they never embed SMTP
credentials. Configure `.env` in the service working directory or supply variables
through the service manager. No service is installed automatically.

## Delivery and records

`.tricycle/alerts/<trajectory-hash>/<incident-hash>/` stores compact evidence,
the validated bilingual `draft.json`, and `delivery.json`. The directory is a local
notification outbox, outside `runs/`. It does not copy candidate pools, checkpoints,
full logs, or full state history. Known environment secrets and credential-like
error strings are redacted before authoring.

A trajectory lock prevents concurrent watchers sending the same incident. A
successful receipt suppresses repeated polls and survives watcher restarts. A new
blocked state revision creates a new incident. If the seed recovers while the
subagent writes, the stale notification is canceled before SMTP delivery.

Authoring, validation, and SMTP failures have a bounded retry budget (default three
attempts, five minutes apart). A validated draft is reused for delivery retries.
Author timeouts terminate the child process group before retrying. Exhaustion
retains the draft/evidence and total attempt count; `--retry-failed` authorizes a
fresh bounded cycle without erasing prior counts. No static fallback email replaces
the requested subagent-authored message. Errors record exception types rather than
raw provider responses. Configuration failures from an integrated watcher are
recorded in `.tricycle/alerts/config-error.json` and do not repeat scientific work.

`sent` means SMTP accepted the message, not that the recipient read it. SMTP cannot
guarantee exactly-once delivery: an interrupted acknowledgement, partial recipient
acceptance, or crash after acceptance can cause a duplicate on retry. A stable
Message-ID and a persisted cooldown reduce duplicate bursts. Mail failure never
resubmits a computation, consumes its scientific retries, or changes its state.
