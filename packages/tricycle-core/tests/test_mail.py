from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tricycle_core import mail


def configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for key in mail.SMTP_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in {"HOST": "smtp.example.test", "USERNAME": "mailer", "PASSWORD": "app-secret",
                       "SENDER": "sender@example.test", "RECIPIENTS": "one@example.test,two@example.test"}.items():
        monkeypatch.setenv("TRICYCLE_SMTP_" + key, value)


@pytest.mark.parametrize("tls,port", [("starttls", 587), ("ssl", 465)])
def test_delivery_uses_environment_routing_and_verified_tls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tls: str, port: int,
) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("TRICYCLE_SMTP_TLS", tls)
    factory = MagicMock()
    client = factory.return_value
    client.send_message.return_value = {}
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL" if tls == "ssl" else "SMTP", factory)
    mail.SmtpSender().send("Blocked / 阻塞", "English status\n中文状态", "<event@tricycle.local>")
    assert factory.call_args.args == ("smtp.example.test", port)
    client.login.assert_called_once_with("mailer", "app-secret")
    message = client.send_message.call_args.args[0]
    assert str(message["To"]) == "one@example.test, two@example.test"
    assert message["From"] == "sender@example.test"
    assert "中文状态" in message.get_content()
    assert message["Message-ID"] == "<event@tricycle.local>"
    if tls == "starttls":
        assert client.starttls.call_args.kwargs["context"].check_hostname
    else:
        assert factory.call_args.kwargs["context"].check_hostname


def test_dotenv_is_lazy_and_environment_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("TRICYCLE_SMTP_HOST=file.example.test\nTRICYCLE_SMTP_PORT=465\n")
    sender = mail.SmtpSender()
    assert sender.settings()["host"] == "smtp.example.test"
    assert sender.settings()["port"] == 465
    monkeypatch.setattr(mail, "load_dotenv", MagicMock(side_effect=AssertionError("unexpected credential read")))
    mail.SmtpSender()


def test_invalid_routing_and_plaintext_fail_before_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    configure(monkeypatch, tmp_path)
    connection = MagicMock()
    monkeypatch.setattr(mail.smtplib, "SMTP", connection)
    monkeypatch.setenv("TRICYCLE_SMTP_TLS", "none")
    with pytest.raises(ValueError, match="TLS"):
        mail.SmtpSender().send("subject", "body", "<id@test>")
    monkeypatch.setenv("TRICYCLE_SMTP_TLS", "starttls")
    monkeypatch.setenv("TRICYCLE_SMTP_RECIPIENTS", "to@example.test\nBcc: other@example.test")
    with pytest.raises(ValueError, match="addresses"):
        mail.SmtpSender().send("subject", "body", "<id@test>")
    connection.assert_not_called()


def test_existing_balance_configuration_uses_shared_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    for key in mail.SMTP_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    sender = mail.SmtpSender({"host": "relay.test", "sender": "from@test", "recipients": ["to@test"]})
    assert sender.configuration_status() == "configured_delivery_unverified"
    assert sender.credentials() == ("relay.test", None, None)
