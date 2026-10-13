from __future__ import annotations

import csv
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from tricycle_agent.bootstrap import create_capability_catalog
from tricycle_agent.campaign import Campaign, CampaignRequest, file_hash, rows
from tricycle_agent.campaign_cli import CapabilityCampaignExecutor, CommandCampaignAgent
from tricycle_agent.cli import main
from tricycle_agent.execution import CapabilityDispatcher
from tricycle_bohrsubmit import InMemoryBohrGateway
from tricycle_core import ExecutionContext, InvalidRequestError, TaskResult, TaskStatus
from tricycle_core.toml import dump_toml, load_toml


def tsv(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(records)


def batch_at(path: Path, *, submitted: bool = True) -> Path:
    path.mkdir()
    dump_toml(path / "run.toml", {"run": {"id": path.name}})
    dump_toml(path / "batch.toml", {"project_id": "test"})
    inputs = path / "worker-001"
    inputs.mkdir()
    (inputs / "input.gjf").write_text("test calculation")
    plan = {"worker": "worker-001", "tasks": 1, "input_dir": str(inputs)}
    tsv(path / "submit-plan.tsv", [plan])
    tsv(
        path / "submit-results.tsv",
        [
            {
                **plan,
                "job_id": "job-1" if submitted else "",
                "status": "SUBMITTED" if submitted else "PREPARED",
            }
        ],
    )
    return path


class Executor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.result = "SUCCEEDED"
        self.bad_download = False
        self.crash_submit = False
        self.accept_before_crash = False

    def execute(self, capability: str, request: dict[str, Any], operation_dir: Path) -> TaskResult:
        self.calls.append(capability)
        if capability == "exploration.round":
            return CapabilityCampaignExecutor().execute(capability, request, operation_dir)
        batch = Path(request["run_dir"])
        if capability == "bohr.batch.status":
            tsv(
                batch / "status.tsv",
                [
                    {
                        "worker": "worker-001",
                        "terminal": "yes" if self.result in {"SUCCEEDED", "FAILED"} else "no",
                        "result": self.result,
                    }
                ],
            )
        elif capability == "bohr.batch.download":
            destination = batch / "downloads"
            destination.mkdir(exist_ok=True)
            (destination / "result.log").write_text("result")
            (destination / "download-manifest.txt").write_text(
                "missing.log" if self.bad_download else "result.log"
            )
            tsv(
                batch / "download-results.tsv",
                [
                    {
                        "worker": "worker-001",
                        "status": "DOWNLOADED",
                        "download_dir": str(destination),
                    }
                ],
            )
        elif capability == "bohr.batch.submit":
            if not self.crash_submit or self.accept_before_crash:
                tsv(
                    batch / "submit-results.tsv",
                    [
                        {**row, "job_id": "next-job", "status": "SUBMITTED"}
                        for row in rows(batch / "submit-plan.tsv")
                    ],
                )
            if self.crash_submit:
                raise RuntimeError("connection lost after submit")
        return TaskResult("test", "test", TaskStatus.SUCCEEDED)


class Agent:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, task_path: Path, response_path: Path, request: CampaignRequest) -> None:
        task = load_toml(task_path)
        self.calls.append(task["phase"])
        response: dict[str, Any] = {"task_sha256": file_hash(task_path)}
        if task["phase"] == "analyzing":
            source = task_path.parent / "proposal.toml"
            dump_toml(
                source,
                {
                    "exploration_id": request.campaign_id,
                    "round_index": task["round_index"],
                    "strategy": "agent-authored-acquisition",
                    "budget": 1,
                    "space": {"space_id": "test"},
                    "model_snapshot": {"snapshot_id": "model-1", "checkpoint_hash": "sha256:model"},
                    "observed_metrics": {"mae": 0.2},
                    "metadata": {
                        "agent_acquisition": {
                            "schema": "tricycle.agent-acquisition/v1",
                            "function_id": "test",
                            "version": "1",
                            "authored_by": "agent-authored-acquisition",
                            "rationale": "test fixture",
                            "expression": "(sampled_novelty + context.model_metrics.mae * context.sampled_distribution.gap) / estimated_cost",
                            "cost_expression": "product_count * mean_product_atom_count",
                            "context": {
                                "sampled_distribution": {"novelty_definition": "test", "gap": 1},
                                "model_metrics": {"mae": 0.2},
                            },
                            "evidence": {
                                **load_toml(Path(task["evidence_path"])),
                                "sampled_set": "sha256:test",
                                "candidate_space": "sha256:test",
                                "model_evaluation": "sha256:test",
                            },
                        }
                    },
                },
            )
            with source.open("a") as handle:
                handle.write(
                    '\n[[candidates]]\ncandidate_id = "candidate-1"\n[candidates.payload]\nproduct_count = 1\nmean_product_atom_count = 10\nsampled_novelty = 0.5\n'
                )
            response.update(request_path=str(source), request_sha256=file_hash(source))
        else:
            batch = batch_at(task_path.parent / "prepared-batch", submitted=False)
            response.update(
                batch_run_dir=str(batch),
                plan_sha256=file_hash(batch / "submit-plan.tsv"),
                selection_sha256=task["selection_sha256"],
                candidate_ids=["candidate-1"],
            )
        dump_toml(response_path, response)


class CampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.batch = batch_at(self.root / "batch")
        self.campaign = Campaign.start(
            CampaignRequest("test", str(self.batch), poll_seconds=1, max_failures=2),
            directory=self.root / "campaign",
        )
        self.executor = Executor()
        self.agent = Agent()
        self.clock = 0

    def tick(self, *, allow_submit: bool = True) -> dict[str, Any]:
        self.clock += 100
        # Reopen each time: exercise persistence across process lifetimes.
        return Campaign(self.campaign.directory).tick(
            self.executor, self.agent, live=True, allow_submit=allow_submit, now=self.clock
        )["state"]

    def prepare(self) -> None:
        for phase in ("downloading", "analyzing", "selecting", "preparing", "submitting"):
            self.assertEqual(self.tick()["phase"], phase)

    def test_complete_loop_downloads_last_batch_without_another_agent_call(self) -> None:
        self.prepare()
        self.assertEqual(self.tick()["phase"], "monitoring")
        self.assertEqual(self.tick()["phase"], "downloading")
        state = self.tick()
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(state["new_batches"], 1)
        self.assertEqual(state["selected_count"], 1)
        self.assertEqual(self.agent.calls, ["analyzing", "preparing"])
        self.assertEqual(self.executor.calls.count("bohr.batch.submit"), 1)

    def test_actual_bohr_lifecycle_download_barrier_with_injected_gateway(self) -> None:
        class Gateway(InMemoryBohrGateway):
            def status(self, external_id: str) -> dict[str, str]:
                return {"status": "Finished"}

            def download(self, external_id: str, destination: Path) -> tuple[str, ...]:
                destination.mkdir(parents=True, exist_ok=True)
                artifact = destination / "output.log"
                artifact.write_text("completed")
                return (str(artifact),)

        dispatcher = CapabilityDispatcher(create_capability_catalog(), bohr_gateway=Gateway())

        class LiveExecutor:
            def execute(
                self, capability: str, request: dict[str, Any], operation_dir: Path
            ) -> TaskResult:
                return dispatcher.execute(
                    capability, request, ExecutionContext("test", dry_run=False)
                )

        self.assertEqual(
            self.campaign.tick(LiveExecutor(), live=True, now=100)["state"]["phase"], "downloading"
        )
        self.assertEqual(
            self.campaign.tick(LiveExecutor(), live=True, now=200)["state"]["phase"], "analyzing"
        )

    def test_selection_replay_does_not_reinvoke_agent_or_change_formula(self) -> None:
        for _ in range(4):
            self.tick()
        saved = self.campaign.read()
        selection = Path(saved["state"]["selection_path"])
        expected = file_hash(selection)
        saved["state"]["phase"] = "selecting"
        self.campaign.save(saved)
        self.assertEqual(self.tick()["phase"], "preparing")
        self.assertEqual(file_hash(selection), expected)
        self.assertEqual(self.agent.calls, ["analyzing"])

    def test_running_batch_never_invokes_agent_and_poll_time_is_respected(self) -> None:
        self.executor.result = "RUNNING"
        self.assertEqual(self.tick()["phase"], "monitoring")
        self.campaign.tick(self.executor, self.agent, live=True, now=self.clock)
        self.assertEqual(self.executor.calls, ["bohr.batch.status"])
        self.assertEqual(self.agent.calls, [])

    def test_failed_workers_are_downloaded_but_missing_artifacts_block_analysis(self) -> None:
        self.executor.result = "FAILED"
        self.tick()
        self.executor.bad_download = True
        self.assertEqual(self.tick()["phase"], "downloading")
        self.assertEqual(self.tick()["phase"], "blocked")
        self.assertEqual(self.agent.calls, [])
        self.executor.bad_download = False
        self.campaign.resume()
        self.assertEqual(self.tick()["phase"], "analyzing")

    def test_uncertain_submit_is_not_retried(self) -> None:
        self.prepare()
        self.executor.crash_submit = True
        self.assertEqual(self.tick()["phase"], "blocked")
        self.campaign.resume()
        self.assertEqual(self.tick()["phase"], "blocked")
        self.assertEqual(self.executor.calls.count("bohr.batch.submit"), 1)

    def test_accepted_submission_is_reconciled_after_crash(self) -> None:
        self.prepare()
        self.executor.crash_submit = self.executor.accept_before_crash = True
        self.assertEqual(self.tick()["phase"], "blocked")
        self.campaign.resume()
        self.assertEqual(self.tick()["phase"], "monitoring")
        self.assertEqual(self.executor.calls.count("bohr.batch.submit"), 1)

    def test_submission_requires_authorization_and_unchanged_inputs(self) -> None:
        self.prepare()
        state = self.tick(allow_submit=False)
        self.assertTrue(state["awaiting_submission_authorization"])
        self.assertNotIn("bohr.batch.submit", self.executor.calls)
        (Path(state["next_batch"]) / "worker-001" / "input.gjf").write_text("changed")
        self.assertIn("inputs changed", self.tick()["reason"])
        self.assertNotIn("bohr.batch.submit", self.executor.calls)

    def test_dry_run_pause_and_lock_do_not_execute_work(self) -> None:
        before = self.campaign.path.read_bytes()
        self.campaign.tick(self.executor, self.agent)
        self.assertEqual(before, self.campaign.path.read_bytes())
        self.campaign.pause()
        self.assertTrue(self.campaign.tick(self.executor, live=True)["paused"])
        self.assertEqual(self.executor.calls, [])
        self.campaign.resume()
        with self.campaign.lock(), self.assertRaises(InvalidRequestError):
            self.campaign.tick(self.executor, live=True)

    def test_handoff_and_stale_agent_response(self) -> None:
        self.tick()
        self.tick()
        result = self.campaign.tick(self.executor, live=True, now=300)
        task = Path(result["state"]["agent_task_path"])
        dump_toml(
            task.parent / "response.toml",
            {"task_sha256": "stale", "action": "stop", "reason": "done"},
        )
        state = self.campaign.tick(self.executor, live=True, now=400)["state"]
        self.assertIn("task hash differs", state["reason"])
        self.assertEqual(state["phase"], "analyzing")

    def test_cli_dry_watch_never_loads_adapter_or_spawns_process(self) -> None:
        with (
            patch(
                "tricycle_agent.campaign_cli.CapabilityCampaignExecutor", side_effect=AssertionError
            ),
            patch("subprocess.Popen", side_effect=AssertionError),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                main(["campaign", "watch", "--run-dir", str(self.campaign.directory)]), 0
            )

    def test_command_adapter_receives_paths_without_shell_expansion(self) -> None:
        task = self.root / "task with space.toml"
        output = self.root / "response.toml"
        dump_toml(task, {"test": True})
        command = (
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[2]).write_text('ok = true\\n')",
        )
        CommandCampaignAgent().run(
            task, output, CampaignRequest("test", str(self.batch), agent_command=command)
        )
        self.assertEqual(load_toml(output), {"ok": True})


if __name__ == "__main__":
    unittest.main()
