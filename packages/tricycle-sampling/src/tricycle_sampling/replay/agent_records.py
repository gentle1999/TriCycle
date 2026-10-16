"""Physical agent handoff with streamed, transactional logs and durable intent."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from contextlib import suppress
from pathlib import Path

from tricycle_core import record_io as rio

from .decision import DecisionOutputError, DecisionTimeoutError
from .runtime_records import work_directory


def invoke(directory: Path, prefix: str, argv: list[str], prompt: str, *, timeout: int, task_sha256: str) -> None:
    """Only schema/output handoff files are expanded; logs go straight to SQLite.

    If the controller is killed, intent, process identity and all committed log
    chunks survive. Handoff files stay at the recorded path for reconciliation;
    an inflight marker never silently authorizes another invocation.
    """
    workspace = work_directory(directory.parent, "review-agent" if prefix else "agent")
    workspace.mkdir(parents=True, exist_ok=True)
    schema = workspace / "response-schema.json"
    output = workspace / "response.pending.json"
    if output.exists():
        raise ValueError("Unreconciled agent output remains in the handoff workspace")
    schema.write_bytes(rio.read_bytes(directory / f"{prefix}response-schema.json"))
    command = list(argv)
    for flag, path in (("--cd", workspace), ("--output-schema", schema), ("--output-last-message", output)):
        command[command.index(flag) + 1] = str(path)
    marker = directory / f"{prefix}agent-inflight.json"
    if rio.exists(marker):
        raise ValueError("Agent invocation is already inflight; reconcile before retry")
    rio.write_json(marker, {"argv": command, "task_sha256": task_sha256,
                           "started_at": time.time(), "work_directory": str(workspace)})
    started = time.monotonic()
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
    rio.write_json(directory / f"{prefix}agent-process.json", {"pid": process.pid, "argv": command})
    errors: list[BaseException] = []

    def capture() -> None:
        assert process.stdout is not None
        try:
            while chunk := os.read(process.stdout.fileno(), 65536):
                rio.append_bytes(directory / f"{prefix}agent.log", chunk)
        except BaseException as error:
            errors.append(error)
        finally:
            process.stdout.close()

    reader = threading.Thread(target=capture, name="trajectory-agent-log", daemon=True)
    reader.start()
    def feed() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(prompt.encode())
        except BrokenPipeError:
            pass  # Exit status/output remain authoritative if the child exits early.
        finally:
            with suppress(BrokenPipeError):
                process.stdin.close()

    sender = threading.Thread(target=feed, name="trajectory-agent-input", daemon=True)
    sender.start()
    failure: BaseException | None = None
    try:
        process.wait(timeout=timeout)
    except BaseException as error:
        failure = error
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    finally:
        sender.join(timeout=10)
        reader.join(timeout=10)
        if reader.is_alive() or sender.is_alive():
            raise RuntimeError("Agent log stream is still active; handoff retained for recovery")
        if errors:
            raise RuntimeError("Agent log could not be persisted; handoff retained") from errors[0]
        with rio.transaction(directory):
            if output.exists():
                rio.atomic_write_bytes(directory / f"{prefix}response.pending.json", output.read_bytes())
            record = {"pid": process.pid, "returncode": process.returncode,
                      "elapsed_seconds": time.monotonic() - started, "finished_at": time.time()}
            if failure:
                record["error"] = str(failure)
            rio.write_json(directory / f"{prefix}agent-exit.json", record)
        # Exact output bytes and final process status have committed before cleanup.
        shutil.rmtree(workspace)
    if failure:
        if isinstance(failure, subprocess.TimeoutExpired):
            raise DecisionTimeoutError("agent exceeded its time limit", review_pass=bool(prefix)) from failure
        raise failure
    if process.returncode:
        log_path = directory / f"{prefix}agent.log"
        log = rio.read_text(log_path, errors="replace").lower() if rio.exists(log_path) else ""
        if any(text in log for text in ("401 unauthorized", "incorrect api key", "token has expired", "refresh token has expired")):
            raise RuntimeError("decision authentication failed (401); refresh Codex login before resuming; this is not a formula error")
        if any(text in log for text in ("timed out", "deadline exceeded", "upstream_timeout")):
            raise DecisionTimeoutError("agent transport timed out", review_pass=bool(prefix))
        raise DecisionOutputError(f"agent failed ({process.returncode}); inspect {log_path}")


def recover_finished(directory: Path, *, review: bool = False) -> Path:
    """Import a finished interrupted invocation without calling the model again.

    This explicit recovery operation retains the marker in the audit namespace.
    Response validation/compilation remains the controller's responsibility.
    """
    prefix = "review-" if review else ""
    marker = directory / f"{prefix}agent-inflight.json"
    invocation = json.loads(rio.read_text(marker))
    if invocation["task_sha256"] != rio.file_sha256(directory / "task.json"):
        raise ValueError("Interrupted invocation task binding changed")
    process = json.loads(rio.read_text(directory / f"{prefix}agent-process.json"))
    try:
        os.kill(process["pid"], 0)
    except ProcessLookupError:
        pass
    else:
        raise ValueError("Recorded agent PID is still present; reconcile identity before recovery")
    workspace = Path(invocation["work_directory"])
    pending = workspace / "response.pending.json"
    data = pending.read_bytes()
    json.loads(data)  # Incomplete output is evidence, never an accepted response.
    response = directory / f"{prefix}response.pending.json"
    with rio.transaction(directory):
        rio.atomic_write_bytes(response, data)
        rio.write_json(directory / f"{prefix}agent-exit.json", {
            "pid": process["pid"], "returncode": None, "recovered_output": True,
            "note": "Process is absent; exit code is unknown. Validate the completed response before use.",
        })
        rio.write_json(directory / f"{prefix}recovered-output.json", {
            "task_sha256": invocation["task_sha256"], "sha256": rio.file_sha256(response),
            "original_invocation": invocation,
        })
    return response
