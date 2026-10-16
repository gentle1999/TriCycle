import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from tricycle_core.io import write_json
from tricycle_sampling.replay import alerts
from tricycle_sampling.replay.alert_author import CommandAlertAuthor


def fixture(tmp_path: Path) -> tuple[alerts.AlertConfig, Path, Path, dict]:
    root = tmp_path / "experiment"
    seed = root / "seed-00"
    state = {"seed": 0, "round": 2, "phase": "blocked", "blocked_phase": "agent",
             "revision": 10, "last_error": "Decision timeout", "failures": 3, "selected_ids": ["a"]}
    write_json(seed / "state.json", state)
    write_json(root / "experiment.json", {"experiment_id": "test", "sampling_strategy": "intelligent"})
    config = alerts.AlertConfig(tmp_path / "alerts", 10, 3, 1,
                                CommandAlertAuthor(("unused",), "test", "low", 10))
    return config, root, seed, state


def draft(evidence: dict) -> dict[str, str]:
    return {"evidence_id": evidence["evidence_id"], "subject_en": "Trajectory blocked",
            "subject_zh": "轨迹阻塞", "body_en": "Observed timeout; inspect the recorded failure.",
            "body_zh": "已观测到超时；请检查失败记录。"}


def test_same_block_sends_once_and_new_revision_can_alert_again(tmp_path: Path) -> None:
    config, root, seed, state = fixture(tmp_path)
    author, sender = Mock(), Mock()
    author.draft.side_effect = draft
    for _ in range(2):
        assert alerts.deliver(config, root, seed, state, author=author, sender=sender)["status"] == "sent"
    author.draft.assert_called_once()
    sender.send.assert_called_once()
    assert "中文" in sender.send.call_args.args[1]
    state["revision"] += 2
    write_json(seed / "state.json", state)
    assert alerts.deliver(config, root, seed, state, author=author, sender=sender)["status"] == "sent"
    assert sender.send.call_count == 2
    assert alerts.read(seed / "state.json") == state


def test_mail_retries_reuse_draft_and_restart_keeps_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, root, seed, state = fixture(tmp_path)
    now = [100.0]
    monkeypatch.setattr(alerts.time, "time", lambda: now[0])
    author, sender = Mock(), Mock()
    author.draft.side_effect = draft
    sender.send.side_effect = TimeoutError("secret server response")
    record: dict = {}
    for index in range(3):
        now[0] = 100.0 + index * 11
        record = alerts.deliver(config, root, seed, state, author=author, sender=sender)
        assert record["attempts"] == index + 1
        alerts.deliver(config, root, seed, state, author=author, sender=sender)
        assert sender.send.call_count == index + 1
    assert record["status"] == "exhausted"
    author.draft.assert_called_once()
    assert len({call.args[2] for call in sender.send.call_args_list}) == 1
    assert "secret server response" not in "".join(p.read_text() for p in config.state_dir.rglob("*.json"))
    sender.send.side_effect = None
    resumed = alerts.deliver(config, root, seed, state, author=author, sender=sender, retry_failed=True)
    assert resumed["status"] == "sent" and resumed["attempts"] == 4
    assert resumed["cycle_attempts"] == 1


def test_bad_author_output_never_sends_and_can_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, root, seed, state = fixture(tmp_path)
    now = [100.0]
    monkeypatch.setattr(alerts.time, "time", lambda: now[0])
    author, sender = Mock(), Mock()
    author.draft.return_value = {"subject_en": "incomplete"}
    assert alerts.deliver(config, root, seed, state, author=author, sender=sender)["status"] == "retry-pending"
    sender.send.assert_not_called()
    now[0] += 11
    author.draft.side_effect = draft
    assert alerts.deliver(config, root, seed, state, author=author, sender=sender)["status"] == "sent"


def test_recovery_during_authoring_cancels_stale_email(tmp_path: Path) -> None:
    config, root, seed, state = fixture(tmp_path)
    author, sender = Mock(), Mock()

    def recover(facts: dict) -> dict[str, str]:
        write_json(seed / "state.json", {**state, "phase": "agent", "revision": 11})
        return draft(facts)

    author.draft.side_effect = recover
    assert alerts.deliver(config, root, seed, state, author=author, sender=sender)["status"] == "resolved"
    sender.send.assert_not_called()


def test_dry_run_never_loads_credentials_or_invokes_subagent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, root, seed, state = fixture(tmp_path)
    monkeypatch.setattr(alerts, "load_dotenv", Mock(side_effect=AssertionError("credential read")))
    monkeypatch.setattr(CommandAlertAuthor, "draft", Mock(side_effect=AssertionError("agent call")))
    monkeypatch.setattr(alerts.SmtpSender, "send", Mock(side_effect=AssertionError("mail call")))
    assert alerts.scan(root)[0]["status"] == "dry-run"
    assert not alerts.notify_blocked(root, seed, state, live=False)
    assert not config.state_dir.exists()


def test_concurrent_watcher_cannot_duplicate_delivery(tmp_path: Path) -> None:
    config, root, seed, state = fixture(tmp_path)
    author, sender = Mock(), Mock()

    def nested(facts: dict) -> dict[str, str]:
        result = alerts.deliver(config, root, seed, state, author=author, sender=sender)
        assert result["status"] == "busy"
        return draft(facts)

    author.draft.side_effect = nested
    assert alerts.deliver(config, root, seed, state, author=author, sender=sender)["status"] == "sent"
    sender.send.assert_called_once()


def test_redaction_and_compact_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, root, seed, state = fixture(tmp_path)
    monkeypatch.setenv("BOHR_ACCESS_KEY", "secret-value")
    state["last_error"] = "Bearer other-secret token=token-secret failure secret-value"
    facts = alerts.evidence(root, seed, state)
    serialized = json.dumps(facts)
    for secret in ("other-secret", "token-secret", "secret-value"):
        assert secret not in serialized
    assert "selected_ids" not in facts and facts["sampled_pair_count"] == 1


def test_nonblocked_state_has_no_notification_side_effect(tmp_path: Path) -> None:
    config, root, seed, state = fixture(tmp_path)
    author, sender = Mock(), Mock()
    state["phase"] = "monitor"
    assert alerts.deliver(config, root, seed, state, author=author, sender=sender)["status"] == "not-blocked"
    author.draft.assert_not_called()
    sender.send.assert_not_called()
    assert not config.state_dir.exists()


def test_supervisor_hook_keeps_only_pending_notification_retries_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, root, seed, state = fixture(tmp_path)
    monkeypatch.setattr(alerts, "live_config", lambda: config)
    delivery = Mock(side_effect=[{"status": "retry-pending"}, {"status": "exhausted"}])
    monkeypatch.setattr(alerts, "deliver", delivery)
    assert alerts.notify_blocked(root, seed, state, live=True)
    assert not alerts.notify_blocked(root, seed, state, live=True)
    assert alerts.read(seed / "state.json") == state


def test_bad_configuration_isolated_from_scientific_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, root, seed, state = fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(alerts, "live_config", Mock(side_effect=ValueError("private detail")))
    assert not alerts.notify_blocked(root, seed, state, live=True)
    receipt = alerts.read(tmp_path / ".tricycle/alerts/config-error.json")
    assert receipt["error_type"] == "ValueError"
    assert "private detail" not in json.dumps(receipt)
    assert alerts.read(seed / "state.json") == state


def test_live_requires_explicit_enable_and_dry_run_validates_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, root, _, _ = fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRICYCLE_TRAJECTORY_ALERTS_ENABLED", "false")
    assert alerts.scan(root, live=True)[0]["status"] == "disabled"
    missing = tmp_path / "missing.toml"
    with pytest.raises(FileNotFoundError):
        alerts.scan(root, config_path=missing)
    with pytest.raises(ValueError, match="existing, unpacked"):
        alerts.scan(tmp_path / "missing-run")
