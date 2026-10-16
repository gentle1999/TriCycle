"""Injectable TLS SMTP delivery shared by task and host-service notifications."""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from pathlib import Path
from typing import Any, Protocol

from .env import load_dotenv

SMTP_ENV_KEYS = frozenset({
    "TRICYCLE_SMTP_HOST", "TRICYCLE_SMTP_PORT", "TRICYCLE_SMTP_TLS",
    "TRICYCLE_SMTP_USERNAME", "TRICYCLE_SMTP_PASSWORD", "TRICYCLE_SMTP_SENDER",
    "TRICYCLE_SMTP_RECIPIENTS", "TRICYCLE_SMTP_TIMEOUT_SECONDS",
})


class MailSender(Protocol):
    def send(self, subject: str, body: str, message_id: str) -> None: ...


class SmtpSender:
    """Resolve credentials only on live use; process environment takes precedence.

    A configuration mapping preserves existing balance-monitor routing. New
    callers use environment-only settings, optionally selecting an env_file.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    def settings(self) -> dict[str, Any]:
        config = self.config
        values = dict(os.environ)
        aliases = {key: config.get(f"{key}_env", f"TRICYCLE_SMTP_{key.upper()}")
                   for key in ("host", "username", "password", "sender", "recipients", "port", "tls")}
        load_dotenv(config.get("env_file"), environ=values,
                    keys=SMTP_ENV_KEYS | set(aliases.values()))
        resolved: dict[str, Any] = {key: values.get(env_key) or config.get(key) for key, env_key in aliases.items()}
        if not resolved["password"] and config.get("password_file"):
            resolved["password"] = Path(config["password_file"]).expanduser().read_text().rstrip("\r\n")
        resolved["tls"] = resolved["tls"] or "starttls"
        if resolved["tls"] not in {"ssl", "starttls"}:
            raise ValueError("SMTP TLS must be ssl or starttls")
        resolved["port"] = int(resolved["port"] or (465 if resolved["tls"] == "ssl" else 587))
        resolved["timeout"] = int(values.get("TRICYCLE_SMTP_TIMEOUT_SECONDS") or 30)
        if not 1 <= resolved["port"] <= 65535 or not 1 <= resolved["timeout"] <= 120:
            raise ValueError("Invalid SMTP port or timeout")
        if not resolved["host"] or bool(resolved["username"]) != bool(resolved["password"]):
            raise ValueError("SMTP host and complete credentials must be configured")
        recipients = resolved["recipients"]
        if isinstance(recipients, str):
            recipients = [part.strip() for part in recipients.split(",") if part.strip()]
        if not isinstance(recipients, list) or not recipients:
            raise ValueError("SMTP recipients must be configured")
        resolved["recipients"] = recipients
        for address in [resolved["sender"], *recipients]:
            if (not isinstance(address, str) or "\r" in address or "\n" in address
                    or parseaddr(address)[1] != address or "@" not in address):
                raise ValueError("SMTP sender and recipients must be plain email addresses")
        return resolved

    def credentials(self) -> tuple[str, str | None, str | None]:
        settings = self.settings()
        return settings["host"], settings["username"], settings["password"]

    def configuration_status(self) -> str:
        try:
            self.settings()
        except FileNotFoundError:
            return "missing_password_file"
        except (OSError, ValueError, KeyError):
            return "configuration_incomplete"
        return "configured_delivery_unverified"

    def send(self, subject: str, body: str, message_id: str) -> None:
        settings = self.settings()
        message = EmailMessage()
        message["From"] = settings["sender"]
        message["To"] = ", ".join(settings["recipients"])
        message["Subject"] = subject
        message["Message-ID"] = message_id
        message["Date"] = formatdate(localtime=True)
        message.set_content(body)
        context = ssl.create_default_context()
        if settings["tls"] == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                settings["host"], settings["port"], timeout=settings["timeout"], context=context)
        else:
            client = smtplib.SMTP(settings["host"], settings["port"], timeout=settings["timeout"])
        with client:
            if settings["tls"] == "starttls":
                client.starttls(context=context)
            if settings["username"]:
                client.login(settings["username"], settings["password"])
            if client.send_message(message):
                raise RuntimeError("SMTP rejected one or more recipients")
