"""Deterministic balance monitoring; external access requires --live."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from tricycle_core import timestamped_run_directory
from tricycle_core.io import write_json as save
from tricycle_core.mail import MailSender, SmtpSender

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Balance:
    amount: Decimal
    unit: str
    details: str = ""


class BalanceSource(Protocol):
    def read(self, account: dict[str, Any]) -> Balance: ...


def number(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("balance must be a finite number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("balance must be a finite number") from exc
    if not result.is_finite():
        raise ValueError("balance must be a finite number")
    return result


class JsonCommandSource:
    """Explicit argv and JSON path; never guess a provider's billing API."""

    def read(self, account: dict[str, Any]) -> Balance:
        completed = subprocess.run(
            account["command"], capture_output=True, text=True, timeout=30, check=False,
        )
        if completed.returncode:
            # Provider output can contain keys; do not persist stdout/stderr.
            raise RuntimeError("balance query command failed")
        document = json.loads(completed.stdout, parse_float=Decimal)
        value = document
        for part in account["json_path"]:
            value = value[int(part)] if isinstance(value, list) else value[part]
        if account.get("metric") == "remaining_percent":
            total = document
            for part in account["total_json_path"]:
                total = total[int(part)] if isinstance(total, list) else total[part]
            denominator = number(total)
            if denominator <= 0:
                raise ValueError("total allowance must be positive for percentage alerts")
            return Balance(number(value) / denominator * 100, "%")
        return Balance(number(value) * number(account.get("multiplier", "1")), account["unit"],
                       str(document.get(account["details_field"], "")) if account.get("details_field") else "")


def validate(config: dict[str, Any]) -> None:
    accounts = config.get("accounts", [])
    if not accounts or len({a["name"] for a in accounts}) != len(accounts):
        raise ValueError("configure unique account names")
    if int(config.get("poll_seconds", 300)) < 60:
        raise ValueError("poll_seconds must be at least 60")
    if int(config.get("repeat_seconds", 43200)) < 300:
        raise ValueError("repeat_seconds must be at least 300")
    for account in accounts:
        if not isinstance(account.get("command"), list) or not account["command"] or not all(isinstance(v, str) and v for v in account["command"]):
            raise ValueError("account command must be an explicit non-empty argv array")
        if not isinstance(account.get("json_path"), list) or not account["json_path"]:
            raise ValueError("configure the documented balance JSON field path")
        if number(account["threshold"]) < 0 or number(account.get("multiplier", "1")) <= 0:
            raise ValueError("threshold must be non-negative and multiplier positive")
        if not account.get("unit"):
            raise ValueError("balance unit is required")
        if account.get("metric", "amount") not in ("amount", "remaining_percent"):
            raise ValueError("unsupported balance metric")
        if account.get("metric") == "remaining_percent":
            if account["unit"] != "%" or number(account["threshold"]) > 100:
                raise ValueError("percentage alerts require unit % and threshold <= 100")
            if not isinstance(account.get("total_json_path"), list) or not account["total_json_path"]:
                raise ValueError("percentage alerts require the documented total allowance field")
    smtp = config["smtp"]
    if smtp.get("tls", "starttls") not in ("ssl", "starttls"):
        raise ValueError("SMTP requires ssl or starttls")
    if not (smtp.get("host_env") or smtp.get("host")) or not smtp.get("sender") or not smtp.get("recipients"):
        raise ValueError("configure SMTP host environment name, sender and recipients")
    if not isinstance(smtp["recipients"], list):
        raise ValueError("SMTP recipients must be an array")


def tick(config: dict[str, Any], root: Path, source: BalanceSource, mail: MailSender) -> None:
    """Query each account independently; mail failures cannot stop experiments."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = root / "state.json"
        state = json.loads(path.read_text()) if path.exists() else {"accounts": {}}
        repeat = int(config.get("repeat_seconds", 43200))
        for account in config["accounts"]:
            record = state["accounts"].setdefault(account["name"], {})
            now = time.time()
            record["checked_at"] = now
            try:
                balance = source.read(account)
                if balance.unit != account["unit"]:
                    raise ValueError("balance source unit mismatch")
                record.update(amount=str(balance.amount), unit=balance.unit, query_failures=0)
                record.pop("query_error", None)
                kind = "exhausted" if balance.amount <= 0 else "low" if balance.amount <= number(account["threshold"]) else "healthy"
                label = str(account.get("metric_label", "balance"))
                subject = f"[TriCycle] {account['name']} {label}{' exhausted' if kind == 'exhausted' else ' low'}"
                body = (f"Account: {account['name']}\nCurrent {label}: {balance.amount} {balance.unit}\n"
                        f"Alert threshold: {account['threshold']} {balance.unit}\n{balance.details}\n"
                        "Review the account quota and take action as needed. The monitor does not top up accounts or change experiment budgets.\n"
                        "Quota values come from the provider and may be delayed.")
            except Exception as exc:
                record["query_failures"] = int(record.get("query_failures", 0)) + 1
                record["query_error"] = type(exc).__name__
                kind = "query_failed"
                label = str(account.get("metric_label", "balance"))
                subject = f"[TriCycle] {account['name']} {label} monitoring unavailable"
                body = (f"Account: {account['name']}\nBalance queries failed {record['query_failures']} consecutive times.\n"
                        "The remaining balance is unknown; check the billing endpoint or login status.")
            record["status"] = kind
            if kind == "healthy":
                record.pop("last_alert_kind", None)
            eligible = kind in ("low", "exhausted") or (kind == "query_failed" and record["query_failures"] >= 3)
            due = kind != record.get("last_alert_kind") or now - record.get("last_sent_at", 0) >= repeat
            if eligible and due and now >= record.get("next_delivery_attempt", 0):
                identity = hashlib.sha256(f"{root.resolve()}:{account['name']}:{kind}:{now}".encode()).hexdigest()
                message_id = f"<{identity}@tricycle.local>"
                # Persist the cooldown first: a crash after SMTP acceptance must not send a burst.
                record.update(next_delivery_attempt=now + 900, pending_message_id=message_id)
                save(path, state)
                try:
                    mail.send(subject, body, message_id)
                except Exception as exc:
                    record["mail_error"] = type(exc).__name__
                else:
                    record.update(last_sent_at=now, last_alert_kind=kind, next_delivery_attempt=0)
                    record.pop("mail_error", None)
                    record.pop("pending_message_id", None)
                    save(root / "notifications" / f"{identity}.json",
                         {"account": account["name"], "kind": kind, "sent_at": now,
                          "message_id": message_id, "amount": record.get("amount")})
            save(path, state)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("tick", "watch", "service-file"), default="tick", nargs="?")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    config = tomllib.loads(args.config.read_text())
    validate(config)
    if not args.live:
        print(json.dumps({"mode": "dry-run", "accounts": [
            {"name": a["name"], "threshold": a["threshold"], "unit": a["unit"]}
            for a in config["accounts"]], "recipients": config["smtp"]["recipients"],
            "poll_seconds": config.get("poll_seconds", 300)}, ensure_ascii=False))
        return
    root = (args.run_dir or timestamped_run_directory(ROOT / ".tricycle" / "monitors", "balance-alert")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.action == "service-file":
        payload: dict[str, Any] = {
            "Label": "org.tricycle.balance-alert", "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 60, "WorkingDirectory": str(ROOT),
            "ProgramArguments": [sys.executable, "-m", "tools.balance_alert", "watch",
                                 "--config", str(args.config.resolve()), "--run-dir", str(root), "--live"],
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                **{key: os.environ[key] for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy", "CODEX_HOME") if key in os.environ}},
            "StandardOutPath": str(root / "monitor.log"), "StandardErrorPath": str(root / "monitor-error.log"),
        }
        smtp_host = config["smtp"].get("host") or os.environ.get(config["smtp"].get("host_env", "TRICYCLE_SMTP_HOST"), "")
        if smtp_host:
            environment = payload["EnvironmentVariables"]
            for key in ("NO_PROXY", "no_proxy"):
                entries = [v for v in environment.get(key, "").split(",") if v]
                environment[key] = ",".join(dict.fromkeys([*entries, smtp_host]))
        # Credentials are not embedded in this file. Supply SMTP env through the service manager.
        path = root / "balance-alert.plist"
        path.write_bytes(plistlib.dumps(payload))
        print(path)
        return
    source, mail = JsonCommandSource(), SmtpSender(config["smtp"])
    while True:
        save(root / "mail-status.json", {"status": mail.configuration_status(), "checked_at": time.time()})
        tick(config, root, source, mail)
        if args.action == "tick":
            return
        time.sleep(int(config.get("poll_seconds", 300)))


if __name__ == "__main__":
    main()
