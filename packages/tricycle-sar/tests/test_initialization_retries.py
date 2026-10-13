from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest
from tricycle_core.toml import dump_toml, load_toml

from tricycle_sar.backends.environments import engine
from tricycle_sar.backends.outputs import collect, read


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay


def install(
    tmp_path: Path,
    attempts: list[dict[str, Any]],
    *,
    deadline: float = 1800,
    extra: tuple[str, ...] = (),
) -> str:
    return engine.run_install_command(
        ["python", "-m", "pip", "install", "fixed-package==1.0", *extra],
        log_path=tmp_path / "install.log",
        deadline=deadline,
        attempts=attempts,
        env={
            "PIP_INDEX_URL": "https://mirror.example/simple",
            "PIP_CACHE_DIR": str(tmp_path / "cache"),
        },
    )


def test_mirror_failure_then_timeout_recovers_in_same_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    calls = []

    def run(argv: list[str], **kwargs: Any) -> str:
        calls.append((argv[:], kwargs))
        clock.now += 10
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, argv, stderr="HTTP error 403 Forbidden")
        if len(calls) == 2:
            raise subprocess.TimeoutExpired(argv, 450, stderr="Downloading rdkit wheel")
        return "installed"

    monkeypatch.setattr(engine, "run_logged_command", run)
    attempts: list[dict[str, Any]] = []
    assert install(tmp_path, attempts) == "installed"
    assert clock.delays == [5, 15]
    assert len(calls) == 3
    assert calls[0][0] == ["python", "-m", "pip", "install", "fixed-package==1.0"]
    assert all(command[-2:] == ["--index-url", engine.PYPI_INDEX_URL] for command, _ in calls[1:])
    assert len({kw["env"]["PIP_CACHE_DIR"] for _, kw in calls}) == 1
    assert [entry["status"] for entry in attempts] == ["failed", "failed", "succeeded"]
    assert attempts[-1]["command_elapsed_seconds"] == 50


@pytest.mark.parametrize(
    "output",
    [
        "ResolutionImpossible: conflicting dependencies",
        "Package hash mismatch",
        "No matching distribution found for nonexistent==1.0",
        "CERTIFICATE_VERIFY_FAILED",
        "CUDA out of memory",
        "No space left on device",
        "SyntaxError: invalid syntax",
        "HTTP error 403; package hash mismatch",
    ],
)
def test_deterministic_failure_does_not_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    def run(argv: list[str], **kwargs: Any) -> str:
        raise subprocess.CalledProcessError(1, argv, stderr=output)

    monkeypatch.setattr(engine, "run_logged_command", run)
    attempts: list[dict[str, Any]] = []
    with pytest.raises(engine.InitializationFailure) as caught:
        install(tmp_path, attempts, deadline=time.monotonic() + 1800)
    assert caught.value.next_action == "inspect_error"
    assert len(attempts) == 1
    assert not attempts[0]["retry"]


def test_retries_are_bounded_and_keep_explicit_package_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    commands = []

    def run(argv: list[str], **kwargs: Any) -> str:
        commands.append(argv[:])
        raise subprocess.CalledProcessError(1, argv, stderr="Connection reset by peer")

    monkeypatch.setattr(engine, "run_logged_command", run)
    attempts: list[dict[str, Any]] = []
    with pytest.raises(engine.InitializationFailure) as caught:
        install(tmp_path, attempts, extra=("--index-url", "https://fixed.example/simple"))
    assert caught.value.reason == "initialization_network_exhausted"
    assert caught.value.next_action == "retry_job"
    assert len(commands) == 3
    assert all(command == commands[0] for command in commands)
    assert clock.delays == [5, 15]
    assert not attempts[-1]["retry"]


def test_total_budget_limits_command_timeouts_and_prevents_more_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    timeouts = []

    def run(argv: list[str], **kwargs: Any) -> str:
        timeouts.append(kwargs["timeout"])
        clock.now += kwargs["timeout"]
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(engine, "run_logged_command", run)
    attempts: list[dict[str, Any]] = []
    with pytest.raises(engine.InitializationFailure):
        install(tmp_path, attempts, deadline=500)
    assert timeouts == [450, 45]
    assert clock.now == 500
    with pytest.raises(engine.InitializationFailure, match="budget exhausted"):
        install(tmp_path, attempts, deadline=500)
    assert len(timeouts) == 2


def test_training_commands_and_official_403_do_not_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(argv: list[str], **kwargs: Any) -> str:
        raise subprocess.CalledProcessError(1, argv, stderr="HTTP error 403 Forbidden")

    monkeypatch.setattr(engine, "run_logged_command", run)
    attempts: list[dict[str, Any]] = []
    with pytest.raises(engine.InitializationFailure):
        install(
            tmp_path,
            attempts,
            deadline=time.monotonic() + 1800,
            extra=("--index-url", engine.PYPI_INDEX_URL),
        )
    assert len(attempts) == 1

    def training(argv: list[str], **kwargs: Any) -> str:
        raise subprocess.CalledProcessError(1, argv, stderr="ConnectionError in model code")

    monkeypatch.setattr(engine, "run_logged_command", training)
    attempts.clear()
    with pytest.raises(engine.InitializationFailure) as caught:
        engine.run_install_command(
            ["python", "train.py"],
            log_path=tmp_path / "log",
            env={},
            deadline=time.monotonic() + 1800,
            attempts=attempts,
        )
    assert caught.value.next_action == "inspect_error"
    assert len(attempts) == 1


def test_empty_mirror_falls_back_once_but_missing_official_version_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    commands = []

    def run(argv: list[str], **kwargs: Any) -> str:
        commands.append(argv[:])
        raise subprocess.CalledProcessError(
            1,
            argv,
            stderr=(
                "Could not find a version that satisfies the requirement fixed-package==1.0 (from versions: none)\n"
                "No matching distribution found for fixed-package==1.0"
            ),
        )

    monkeypatch.setattr(engine, "run_logged_command", run)
    attempts: list[dict[str, Any]] = []
    with pytest.raises(engine.InitializationFailure) as caught:
        install(tmp_path, attempts)
    assert caught.value.next_action == "inspect_error"
    assert len(commands) == 2
    assert commands[-1][-2:] == ["--index-url", engine.PYPI_INDEX_URL]
    assert clock.delays == [5]


@pytest.mark.parametrize(
    "output",
    [
        "HTTP error 503",
        "502 Server Error: Bad Gateway",
        "status code: 429",
        "curl: (22) The requested URL returned error: 503",
        "ReadTimeoutError",
    ],
)
def test_transient_transport_errors_are_recognized(output: str) -> None:
    assert engine.retryable_install_failure(subprocess.CalledProcessError(1, ["pip"]), output)


def test_log_classification_uses_only_current_command(tmp_path: Path) -> None:
    log = tmp_path / "install.log"
    log.write_text("Previous command: ConnectionError\n")
    with pytest.raises(subprocess.CalledProcessError) as caught:
        engine.run_logged_command(
            [
                sys.executable,
                "-c",
                "import sys; print('Invalid version', file=sys.stderr); sys.exit(1)",
            ],
            log_path=log,
            env=dict(os.environ),
            cwd=None,
            capture=True,
            timeout=10,
        )
    assert "Invalid version" in caught.value.stderr
    assert "ConnectionError" not in caught.value.stderr
    assert not engine.retryable_install_failure(caught.value, caught.value.stderr)


def test_timeout_reaps_child_processes_before_retry(tmp_path: Path) -> None:
    marker = tmp_path / "orphan-output"
    child = "import time,pathlib; time.sleep(1); pathlib.Path(" + repr(str(marker)) + ").touch()"
    parent = (
        "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
        + repr(child)
        + "]); time.sleep(10)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        engine.run_logged_command(
            [sys.executable, "-c", parent],
            log_path=tmp_path / "log",
            env=dict(os.environ),
            cwd=None,
            capture=False,
            timeout=0.2,
        )
    time.sleep(1.1)
    assert not marker.exists()


@pytest.mark.parametrize(
    "error",
    [
        "The NVIDIA driver on your system is too old (found version 12000)",
        "Found no NVIDIA driver on your system",
        "base Torch differs from configured runtime",
        "CUDA driver/library version mismatch",
    ],
)
def test_cuda_error_text_does_not_infer_a_bad_node(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: str,
) -> None:
    report = {
        "passed": False, "error": error,
        "python": "/model/env/bin/python", "torch_file": "/image/torch/__init__.py",
        "loaded_cuda_libraries": ["/unexpected/libcuda.so.1"],
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kw: subprocess.CompletedProcess(
            args[0],
            0,
            stdout=json.dumps(report),
            stderr="",
        ),
    )
    with pytest.raises(engine.InitializationFailure) as caught:
        engine.cuda_preflight({}, "2.12.1+cu130")
    assert caught.value.next_action == "inspect_error"
    assert caught.value.reason == "cuda_preflight_failed"
    assert json.loads(capsys.readouterr().out.removeprefix("[init-preflight] ")) == report


def test_preflight_uses_selected_interpreter_and_preserves_loading_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # Execute the actual subprocess probe with a broken runtime. Diagnostics must
    # survive the import failure, including on a host with no NVIDIA installation.
    (tmp_path / "torch.py").write_text("raise ImportError('broken torch dependency')\n")
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "PYTHONNOUSERSITE": "1"}
    with pytest.raises(engine.InitializationFailure, match="broken torch dependency"):
        engine.cuda_preflight(env, "test-runtime")
    report = json.loads(capsys.readouterr().out.removeprefix("[init-preflight] "))
    assert report["python"] == sys.executable
    assert report["environment"]["PYTHONPATH"] == str(tmp_path)
    assert "nvidia_smi" in report or "nvidia_smi_error" in report


def test_cuda_failure_is_recorded_before_installation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine, "HERE", tmp_path)
    (tmp_path / "recipes.toml").write_text("[framework]\n")
    directory = tmp_path / "environment"
    directory.mkdir()

    def preflight(*args: Any) -> dict[str, Any]:
        raise engine.InitializationFailure("cuda_preflight_failed", "inspect_error", "driver too old")

    def unexpected(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("Started installation after a failed runtime check")

    monkeypatch.setattr(engine, "cuda_preflight", preflight)
    monkeypatch.setattr(engine, "run_logged_command", unexpected)
    with pytest.raises(engine.InitializationFailure):
        engine.install_locked(
            Namespace(model="rxnresid"),
            {"model": "rxnresid", "torch": "2.12.1+cu130"},
            {"mode": "base-venv"},
            directory,
        )
    state = load_toml(directory / "state.toml")
    assert state["status"] == "failed"
    assert not state["reschedule_required"]
    assert state["next_action"] == "inspect_error"
    assert state["initialization_attempts"] == []


def test_compact_status_retains_recovery_details_and_earlier_results(tmp_path: Path) -> None:
    result, bundle = tmp_path / "results", tmp_path / "bundle"
    result.mkdir()
    (result / "status.csv").write_text("model,fold,status,exit_code\nrxnresid,0,complete,0\n")
    task = tmp_path / "task.toml"
    dump_toml(task, {"model": "rxnresid", "fold": 1})
    dump_toml(
        result / "rxnresid-state.toml",
        {
            "model": "rxnresid",
            "status": "failed",
            "failure_reason": "cuda_node_incompatible",
            "next_action": "reschedule",
            "initialization_attempts": [],
        },
    )
    collect(bundle, task, result, 1)
    statuses = read(result / "status.csv")
    assert statuses[0]["status"] == "complete"
    assert statuses[0]["next_action"] == ""
    assert statuses[1]["next_action"] == "reschedule"
    assert statuses[1]["failure_reason"] == "cuda_node_incompatible"
    assert not (result / "rxnresid-state.toml").exists()
