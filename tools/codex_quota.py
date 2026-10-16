"""Read Codex allowance through the official app-server protocol; no model calls."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from tools.balance_alert import number


async def query(command: str = "codex") -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        command, "app-server", "--listen", "stdio://",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None

    async def send(message: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()

    async def receive(identifier: int) -> dict[str, Any]:
        assert process.stdout is not None
        while line := await process.stdout.readline():
            message = json.loads(line)
            if message.get("id") != identifier:
                continue
            if "error" in message:
                # Never expose upstream messages that might contain credentials.
                raise RuntimeError("Codex account query failed; check local login")
            return dict(message["result"])
        raise RuntimeError("Codex app-server closed before account response")

    async def exchange() -> dict[str, Any]:
        await send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "tricycle_quota_monitor", "version": "1.0.0"},
        }})
        await receive(1)
        await send({"method": "initialized", "params": {}})
        await send({"id": 2, "method": "account/rateLimits/read"})
        return await receive(2)

    try:
        return await asyncio.wait_for(exchange(), timeout=20)
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()


def normalize(result: dict[str, Any], bucket: str = "codex") -> dict[str, Any]:
    by_id = result.get("rateLimitsByLimitId") or {}
    snapshot = by_id.get(bucket)
    if snapshot is None:
        snapshot = result.get("rateLimits")
        if not isinstance(snapshot, dict) or snapshot.get("limitId") not in (None, bucket):
            raise ValueError("requested Codex quota bucket unavailable")
    windows = []
    for name in ("primary", "secondary"):
        window = snapshot.get(name)
        if window is None:
            continue
        used = number(window["usedPercent"])
        if used < 0:
            raise ValueError("Codex usedPercent must be non-negative")
        remaining = max(number(0), number(100) - used)
        windows.append({
            "window": name, "remaining_percent": str(remaining),
            "window_minutes": window.get("windowDurationMins"),
            "resets_at": window.get("resetsAt"),
        })
    if not windows:
        raise ValueError("Codex did not return usage windows; cannot infer allowance from credits")
    details = []
    for window in windows:
        resets = datetime.fromtimestamp(window["resets_at"], UTC).astimezone().isoformat() if window["resets_at"] is not None else "unknown"
        details.append(f"{window['window']} ({window['window_minutes']}-minute window): {window['remaining_percent']}% remaining, resets at {resets}")
    return {
        "amount": str(min(number(w["remaining_percent"]) for w in windows)),
        "unit": "%", "metric": "codex_allowance_remaining_percent",
        "bucket": bucket, "windows": windows, "details": "\n".join(details),
        "queried_at": datetime.now(UTC).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--codex-command", default="codex")
    parser.add_argument("--bucket", default="codex")
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"mode": "dry-run", "method": "account/rateLimits/read", "bucket": args.bucket}))
        return
    try:
        value = normalize(asyncio.run(query(args.codex_command)), args.bucket)
    except Exception as exc:
        raise SystemExit(f"Codex quota query failed: {type(exc).__name__}") from None
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    main()
