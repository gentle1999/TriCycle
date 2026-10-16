"""Opt-in, reporting-only email alerts for blocked replay trajectories."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import time
import tomllib
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tricycle_core import record_io as rio
from tricycle_core.env import load_dotenv
from tricycle_core.mail import MailSender, SmtpSender
from tricycle_core.record_io import write_json
from tricycle_core.validation import validate_fields

from .alert_author import AlertAuthor, CommandAlertAuthor, validate_draft

CONTROL_KEYS = {"TRICYCLE_TRAJECTORY_ALERTS_ENABLED", "TRICYCLE_TRAJECTORY_ALERT_CONFIG"}


def read(path: Path) -> dict[str, Any]:
    return dict(json.loads(rio.read_text(path)))


@dataclass(frozen=True)
class AlertConfig:
    state_dir: Path
    retry_seconds: int
    max_attempts: int
    poll_seconds: int
    author: CommandAlertAuthor

    @classmethod
    def load(cls, path: Path) -> AlertConfig:
        value = tomllib.loads(rio.read_text(path))
        validate_fields(value, {"state_dir", "retry_seconds", "max_attempts", "poll_seconds", "agent"},
                        context="trajectory alert")
        agent = value["agent"]
        validate_fields(agent, {"command", "model", "reasoning_effort", "timeout_seconds"},
                        context="alert author")
        command = agent["command"]
        if not isinstance(command, list) or not command or not all(isinstance(s, str) and s for s in command):
            raise ValueError("Alert agent command must be a nonempty argv list")
        model, effort = agent["model"], agent["reasoning_effort"]
        if not isinstance(model, str) or not model.strip() or effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("Configure alert agent model and reasoning effort")
        numbers = {"retry_seconds": value.get("retry_seconds", 300),
                   "max_attempts": value.get("max_attempts", 3),
                   "poll_seconds": value.get("poll_seconds", 30),
                   "timeout_seconds": agent.get("timeout_seconds", 120)}
        limits = {"retry_seconds": (10, 86400), "max_attempts": (1, 10),
                  "poll_seconds": (1, 3600), "timeout_seconds": (1, 600)}
        for key, number in numbers.items():
            if type(number) is not int or not limits[key][0] <= number <= limits[key][1]:
                raise ValueError(f"Invalid alert {key}")
        return cls(rio.resolve(Path(value.get("state_dir", ".tricycle/alerts")).expanduser()),
                   numbers["retry_seconds"], numbers["max_attempts"], numbers["poll_seconds"],
                   CommandAlertAuthor(tuple(command), model, effort, numbers["timeout_seconds"]))


def live_config(path: Path | None = None) -> AlertConfig | None:
    values = {key: os.environ[key] for key in CONTROL_KEYS if key in os.environ}
    load_dotenv(keys=CONTROL_KEYS, environ=values)
    enabled = values.get("TRICYCLE_TRAJECTORY_ALERTS_ENABLED", "false").lower()
    if enabled not in {"true", "false"}:
        raise ValueError("TRICYCLE_TRAJECTORY_ALERTS_ENABLED must be true or false")
    if enabled != "true":
        return None
    return AlertConfig.load(path or Path(values.get("TRICYCLE_TRAJECTORY_ALERT_CONFIG")
                                        or "configs/trajectory-alerts.toml"))


def redact(text: str) -> str:
    for key, value in os.environ.items():
        if value and len(value) >= 6 and any(part in key.upper() for part in ("PASSWORD", "TOKEN", "SECRET", "ACCESS_KEY", "API_KEY")):
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", text)
    return re.sub(r"(?i)((?:password|token|secret|access[_-]?key|api[_-]?key)\s*[:=]\s*)[^\s,;]+",
                  r"\1[REDACTED]", text)[:4000]


def evidence(root: Path, seed: Path, state: dict[str, Any]) -> dict[str, Any]:
    config = read(root / "experiment.json") if rio.exists(root / "experiment.json") else {}
    current = seed / f"round-{int(state['round']):04d}"
    facts = {
        "experiment_id": config.get("experiment_id", root.name),
        "strategy": config.get("sampling_strategy", "intelligent"),
        "trajectory": str(rio.resolve(seed)), "seed": state["seed"], "round_index": state["round"],
        "phase": state["phase"], "blocked_phase": state.get("blocked_phase", "unknown"),
        "state_revision": state.get("revision"), "state_updated_at": state.get("updated_at"),
        "external_id": state.get("external_id"), "remote_result": state.get("remote_result"),
        "observed_error": redact(str(state.get("last_error", "No error description was recorded."))),
        "consecutive_failures": state.get("failures", 0),
        "sampled_pair_count": len(state.get("selected_ids", [])),
        "completed_evaluations": len(list(rio.glob(seed, "round-*/evaluation-summary.json"))),
        "decision_rejections": len(list(rio.glob(current / "decision/rejected-attempts", "*"))),
        "decision_timeouts": len(list(rio.glob(current / "decision/timeout-attempts", "*/*/timeout.json"))),
        "submission_timeouts": len(list(rio.glob(current / "submission-timeouts", "*/submission-inflight.json"))),
        "state_path": str(rio.resolve(seed / "state.json")),
        "scientific_actions_by_alert": "none",
    }
    identifier = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()
    return {"evidence_id": identifier, **facts}


def deliver(config: AlertConfig, root: Path, seed: Path, state: dict[str, Any], *,
            author: AlertAuthor, sender: MailSender, retry_failed: bool = False) -> dict[str, Any]:
    """One bounded attempt under a trajectory lock; errors cannot change replay state."""
    if state.get("phase") != "blocked":
        return {"status": "not-blocked"}
    facts = evidence(root, seed, state)
    owner = hashlib.sha256(str(rio.resolve(seed)).encode()).hexdigest()[:24]
    directory = config.state_dir / owner
    rio.mkdir(directory, parents=True, exist_ok=True)
    with rio.open(directory / ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy"}
        event = directory / facts["evidence_id"]
        receipt = event / "delivery.json"
        record = read(receipt) if rio.exists(receipt) else {
            "evidence_id": facts["evidence_id"], "status": "pending", "attempts": 0,
            "cycle_attempts": 0, "message_id": f"<{facts['evidence_id']}@tricycle.local>",
        }
        if record["status"] in {"sent", "resolved"}:
            return record
        if retry_failed and record["cycle_attempts"] >= config.max_attempts:
            record.update(cycle_attempts=0, next_attempt_at=0, retry_requested_at=time.time())
        if record["cycle_attempts"] >= config.max_attempts:
            return {**record, "status": "exhausted"}
        if time.time() < record.get("next_attempt_at", 0):
            return record
        write_json(event / "evidence.json", facts)
        record.update(status="drafting", attempts=record["attempts"] + 1,
                      cycle_attempts=record["cycle_attempts"] + 1,
                      next_attempt_at=time.time() + config.retry_seconds)
        write_json(receipt, record)
        try:
            draft_path = event / "draft.json"
            if rio.exists(draft_path):
                draft = validate_draft(read(draft_path), facts["evidence_id"])
            else:
                draft = validate_draft(author.draft(facts), facts["evidence_id"])
                write_json(draft_path, draft)
            # Another operator may have resumed this seed while the author ran.
            latest = read(seed / "state.json")
            if latest.get("phase") != "blocked" or evidence(root, seed, latest)["evidence_id"] != facts["evidence_id"]:
                record.update(status="resolved", resolved_at=time.time())
            else:
                record.update(status="sending")
                write_json(receipt, record)
                sender.send(f"[TriCycle] {draft['subject_en']} | {draft['subject_zh']}",
                            f"English\n\n{draft['body_en']}\n\n中文\n\n{draft['body_zh']}\n",
                            record["message_id"])
                record.update(status="sent", sent_at=time.time())
                record.pop("error_type", None)
        except Exception as exc:
            record.update(status="exhausted" if record["cycle_attempts"] >= config.max_attempts else "retry-pending",
                          error_type=type(exc).__name__, failed_at=time.time())
        write_json(receipt, record)
        return record


def notify_blocked(root: Path, seed: Path, state: dict[str, Any], *, live: bool) -> bool:
    """Supervisor hook. Return whether a blocked seed has a pending bounded retry."""
    if not live or state.get("phase") != "blocked":
        return False
    try:
        config = live_config()
        if config is None:
            return False
        result = deliver(config, root, seed, state, author=config.author, sender=SmtpSender())
        return result["status"] in {"pending", "drafting", "sending", "retry-pending", "busy"}
    except Exception as exc:
        # Configuration and local notification I/O must never retry scientific work.
        with suppress(OSError):
            write_json(Path(".tricycle/alerts/config-error.json"), {
                "error_type": type(exc).__name__, "at": time.time(),
                "next_action": "Inspect alert configuration and local notification storage.",
            })
        return False


def scan(root: Path, *, live: bool = False, config_path: Path | None = None,
         retry_failed: bool = False) -> list[dict[str, Any]]:
    """Observe existing state without changing experiment/controller bindings."""
    if not rio.is_dir(root):
        raise ValueError("Alert scan requires an existing, unpacked run or seed directory")
    if not live and config_path is not None:
        AlertConfig.load(config_path)
    config = live_config(config_path) if live else None
    reports = []
    states = [root / "state.json"] if rio.exists(root / "state.json") else sorted(rio.rglob(root, "seed-*/state.json"))
    for path in states:
        state = read(path)
        if state.get("phase") != "blocked":
            continue
        seed = path.parent
        if config is None:
            reports.append({"trajectory": str(seed), "status": "disabled" if live else "dry-run",
                            "seed": state.get("seed"), "round": state.get("round")})
        else:
            reports.append(deliver(config, seed.parent, seed, state, author=config.author,
                                   sender=SmtpSender(), retry_failed=retry_failed))
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("scan", "watch"))
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    if args.retry_failed and (args.action != "scan" or not args.live):
        parser.error("--retry-failed requires scan --live")
    while True:
        print(json.dumps(scan(rio.resolve(args.run_dir), live=args.live, config_path=args.config,
                              retry_failed=args.retry_failed)), flush=True)
        if args.action == "scan" or not args.live:
            return
        config = live_config(args.config)
        if config is None:
            return
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    main()
