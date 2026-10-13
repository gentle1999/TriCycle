"""Bounded worker stage with live logs, process-group cleanup and GPU telemetry."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

try:
    from sar_runtime.toml import dump_toml  # pyright: ignore[reportMissingImports]  # isort: skip
except ImportError:
    from tricycle_core.toml import dump_toml


def run_stage(command, *, name, output, timeout, accelerator, progress_timeout=600, evidence=None):
    if evidence and evidence.exists():
        raise FileExistsError(evidence)
    output.mkdir(parents=True, exist_ok=True)
    log_path = output / f"{name}.log"
    state_path = output / f"{name}.toml"
    began = time.monotonic()
    process = None
    observed_evidence = False
    last_progress = began
    evidence_mtime = None

    def state(phase, **extra):
        values = dict(
            stage=name,
            phase=phase,
            accelerator=accelerator,
            elapsed_seconds=round(time.monotonic() - began, 3),
            **extra,
        )
        dump_toml(state_path, values)
        print("[stage] " + json.dumps(values), flush=True)

    def stop():
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    def interrupted(signum, frame):
        raise InterruptedError(f"signal {signum}")

    handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    state("starting")
    try:
        if accelerator == "cuda":
            subprocess.run(["nvidia-smi", "-L"], check=True, timeout=15)
        elif os.environ.get("CUDA_VISIBLE_DEVICES") != "":
            raise RuntimeError("CPU stage requires CUDA_VISIBLE_DEVICES to be empty")
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        selector = selectors.DefaultSelector()
        if process.stdout is None:
            raise RuntimeError("Stage process stdout is unavailable")
        selector.register(process.stdout, selectors.EVENT_READ)
        heartbeat = 0.0
        with log_path.open("wb") as log:
            while selector.get_map():
                elapsed = time.monotonic() - began
                if elapsed > timeout:
                    raise TimeoutError(f"{name}: total stage timeout {timeout}s")
                if evidence and evidence.is_file():
                    # Only an actual model step counts; allocation/preflight does not.
                    proof = tomllib.loads(evidence.read_text())
                    observed_evidence = proof.get("backward_checked") is True
                    mtime = evidence.stat().st_mtime_ns
                    if observed_evidence and mtime != evidence_mtime:
                        last_progress = time.monotonic()
                        evidence_mtime = mtime
                if evidence and time.monotonic() - last_progress > progress_timeout:
                    raise TimeoutError(f"{name}: no real model progress within {progress_timeout}s")
                if time.monotonic() >= heartbeat:
                    state("running", pid=process.pid, actual_model_backward=observed_evidence)
                    if accelerator == "cuda":
                        for fields in [
                            ("gpu", "name,utilization.gpu,memory.used"),
                            ("compute-apps", "pid,process_name,used_memory"),
                        ]:
                            result = subprocess.run(
                                [
                                    "nvidia-smi",
                                    f"--query-{fields[0]}={fields[1]}",
                                    "--format=csv,noheader",
                                ],
                                capture_output=True,
                                timeout=10,
                            )
                            telemetry = result.stdout + result.stderr
                            log.write(telemetry)
                            sys.stdout.buffer.write(telemetry)
                            sys.stdout.buffer.flush()
                    heartbeat = time.monotonic() + 30
                for key, _ in selector.select(timeout=1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        log.write(chunk)
                        log.flush()
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
            code = process.wait(timeout=5)
        selector.close()
        if code:
            raise RuntimeError(f"{name}: child exit {code}; see {log_path}")
        if evidence and (
            not evidence.is_file()
            or tomllib.loads(evidence.read_text()).get("completed") is not True
        ):
            raise RuntimeError("Model exited without complete accelerator evidence")
        state("completed", returncode=code)
    except BaseException as error:
        stop()
        state("failed", error=str(error))
        raise
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--name", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--timeout", type=float, required=True)
    p.add_argument("--accelerator", choices=["cpu", "cuda"], required=True)
    p.add_argument("--progress-timeout", type=float, default=600)
    p.add_argument("--evidence", type=Path)
    p.add_argument("command", nargs=argparse.REMAINDER)
    a = p.parse_args()
    command = a.command[1:] if a.command[:1] == ["--"] else a.command
    run_stage(
        command,
        name=a.name,
        output=a.output,
        timeout=a.timeout,
        accelerator=a.accelerator,
        progress_timeout=a.progress_timeout,
        evidence=a.evidence,
    )


if __name__ == "__main__":
    main()
