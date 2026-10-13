from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from tricycle_core import ExecutionContext, TaskResult, TraceStore
from tricycle_core.toml import dump_toml, load_toml

from .bootstrap import create_capability_catalog
from .campaign import Campaign, CampaignRequest
from .execution import CapabilityDispatcher


class CapabilityCampaignExecutor:
    """Lazy live adapter creation: dry-run and local selection never load credentials."""

    def __init__(self, bohr_config: Path | None = None) -> None:
        self.dispatcher = CapabilityDispatcher(create_capability_catalog())
        self.bohr_config = bohr_config

    def execute(self, capability: str, request: dict[str, Any], operation_dir: Path) -> TaskResult:
        dispatcher = self.dispatcher
        if capability.startswith("bohr."):
            dispatcher = dispatcher.with_bohr_cli(self.bohr_config)
        operation_dir.mkdir(parents=True, exist_ok=True)
        trace = TraceStore(operation_dir, run_id=operation_dir.name)
        context = ExecutionContext(operation_dir.name, Path.cwd(), False, trace_sink=trace)
        trace.emit("campaign.capability.started", capability_id=capability, status="started")
        plan = dispatcher.plan(capability, request, context)
        dispatcher.validate(capability, request, context)
        result = dispatcher.execute(capability, request, context, planned=plan)
        dump_toml(
            operation_dir / "result.toml",
            {"result_json": json.dumps(result.to_dict(), ensure_ascii=False)},
        )
        trace.emit(
            "campaign.capability.finished",
            capability_id=capability,
            status=result.status.value,
            payload=result.to_dict(),
        )
        return result


class CommandCampaignAgent:
    """User-supplied argv receives task and response paths as its last two arguments."""

    def run(self, task_path: Path, response_path: Path, request: CampaignRequest) -> None:
        argv = [*request.agent_command, str(task_path), str(response_path)]
        with (task_path.parent / "agent.log").open("ab") as log:
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True
            )
            dump_toml(task_path.parent / "agent-process.toml", {"pid": process.pid, "argv": argv})
            try:
                code = process.wait(timeout=request.agent_timeout_seconds)
            except BaseException:
                # Stop the process group, including tool subprocesses, before releasing the lock.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                except ProcessLookupError:
                    pass
                raise
        if code:
            raise RuntimeError(f"agent command exited {code}; see {task_path.parent / 'agent.log'}")


def add_campaign_parser(subparsers: Any) -> None:
    campaign = subparsers.add_parser(
        "campaign", help="persist and supervise an adaptive exploration"
    )
    commands = campaign.add_subparsers(dest="campaign_action", required=True)
    start = commands.add_parser(
        "start", help="register an already submitted batch; no remote calls"
    )
    start.add_argument("request", type=Path)
    start.add_argument("--run-dir", type=Path)
    start.add_argument("--workdir", type=Path, default=Path("."))
    for name in ("status", "pause", "resume", "tick", "watch"):
        command = commands.add_parser(name)
        command.add_argument("--run-dir", type=Path, required=True)
        if name in {"tick", "watch"}:
            command.add_argument("--live", action="store_true")
            command.add_argument(
                "--allow-submit",
                action="store_true",
                help="authorize QM submissions within the configured campaign limits",
            )
            command.add_argument("--bohr-config", type=Path)
        if name == "watch":
            command.add_argument(
                "--max-ticks",
                type=int,
                default=0,
                help="0 runs until paused, blocked, complete, or interrupted",
            )


def campaign_command(args: argparse.Namespace) -> int:
    action = args.campaign_action
    if action == "start":
        request = CampaignRequest.from_mapping(load_toml(args.request))
        campaign = Campaign.start(request, directory=args.run_dir, workdir=args.workdir)
        print(
            json.dumps(
                {"campaign_dir": str(campaign.directory), **campaign.read()}, ensure_ascii=False
            )
        )
        return 0
    campaign = Campaign(args.run_dir)
    if action == "pause":
        campaign.pause()
        result = {**campaign.read(), "paused": True}
    elif action == "resume":
        result = campaign.resume()
    elif action == "status":
        result = {**campaign.read(), "paused": (campaign.directory / "pause.toml").exists()}
    else:
        if action == "watch" and args.max_ticks < 0:
            raise ValueError("max-ticks must be nonnegative")
        config = CampaignRequest.from_mapping(campaign.read()["config"])
        executor = CapabilityCampaignExecutor(args.bohr_config) if args.live else None
        agent = CommandCampaignAgent() if args.live and config.agent_command else None
        ticks = 0
        while True:
            result = campaign.tick(executor, agent, live=args.live, allow_submit=args.allow_submit)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            ticks += 1
            state = result["state"]
            if (
                action != "watch"
                or not args.live
                or result.get("paused")
                or state["phase"] in {"complete", "blocked"}
                or (args.max_ticks and ticks >= args.max_ticks)
                or state.get("awaiting_submission_authorization", False)
            ):
                return 1 if state["phase"] == "blocked" else 0
            # Short sleeps make pause responsive even for a long polling interval.
            while time.time() < state["next_check_at"]:
                if (campaign.directory / "pause.toml").exists():
                    break
                time.sleep(min(1.0, max(0.0, state["next_check_at"] - time.time())))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
