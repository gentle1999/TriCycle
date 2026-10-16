import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from tricycle_sampling.replay import alert_author


def test_author_receives_only_reporting_evidence_and_no_smtp_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRICYCLE_SMTP_PASSWORD", "smtp-secret")
    monkeypatch.setenv("TRICYCLE_SMTP_RECIPIENTS", "recipient@example.test")
    process = Mock(returncode=0)
    captured: dict[str, Any] = {}

    def start(argv: list[str], **kwargs: object) -> Mock:
        captured.update(argv=argv, kwargs=kwargs)
        output = Path(argv[argv.index("--output-last-message") + 1])

        def communicate(prompt: bytes, **_: object) -> None:
            captured["prompt"] = prompt.decode()
            output.write_text(json.dumps({"evidence_id": "incident", "subject_en": "Blocked",
                                          "subject_zh": "阻塞", "body_en": "A recorded failure occurred.",
                                          "body_zh": "记录显示发生了失败。"}))

        process.communicate.side_effect = communicate
        return process

    monkeypatch.setattr(alert_author.subprocess, "Popen", start)
    author = alert_author.CommandAlertAuthor(("codex", "exec"), "test-model", "low", 10)
    result = author.draft({"evidence_id": "incident", "observed_error": "Timeout"})
    assert result["subject_zh"] == "阻塞"
    assert "TRICYCLE_SMTP_PASSWORD" not in captured["kwargs"]["env"]
    assert "TRICYCLE_SMTP_RECIPIENTS" not in captured["kwargs"]["env"]
    assert "smtp-secret" not in captured["prompt"]
    assert "Use English for analysis" in captured["prompt"]
    assert "sole language exception" in captured["prompt"]
    output = Path(captured["argv"][captured["argv"].index("--output-last-message") + 1])
    assert not output.parent.exists()


def test_author_timeout_terminates_process_before_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock(returncode=None, pid=12345)
    process.poll.return_value = None
    process.communicate.side_effect = subprocess.TimeoutExpired("author", 1)
    kill = Mock()
    monkeypatch.setattr(alert_author.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(alert_author.os, "killpg", kill)
    author = alert_author.CommandAlertAuthor(("codex", "exec"), "test-model", "low", 1)
    with pytest.raises(subprocess.TimeoutExpired):
        author.draft({"evidence_id": "incident"})
    kill.assert_called_once_with(12345, alert_author.signal.SIGTERM)
    process.wait.assert_called_once_with(timeout=5)


def test_author_schema_rejects_header_injection_and_wrong_evidence() -> None:
    response = {"evidence_id": "incident", "subject_en": "Blocked", "subject_zh": "阻塞",
                "body_en": "Failure", "body_zh": "失败"}
    with pytest.raises(ValueError, match="different evidence"):
        alert_author.validate_draft(response, "other")
    with pytest.raises(ValueError, match="one line"):
        alert_author.validate_draft({**response, "subject_en": "Blocked\nBcc: bad@test"}, "incident")
