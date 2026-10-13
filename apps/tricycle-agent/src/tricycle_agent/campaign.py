"""Persistent application orchestration; no service or model client is implicit."""

from __future__ import annotations

import csv
import fcntl
import math
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from tricycle_core import InvalidRequestError, TaskResult, TaskStatus, timestamped_run_directory
from tricycle_core.io import atomic_write_bytes
from tricycle_core.io import file_sha256 as file_hash
from tricycle_core.toml import dump_toml, load_toml
from tricycle_sampling import validate_calculation_evidence


def freeze_file(source: Path, target: Path, *, replace: bool = False) -> None:
    content = source.read_bytes()
    if target.exists() and not replace:
        if target.read_bytes() != content:
            raise InvalidRequestError(f"immutable artifact differs: {target}")
        return
    atomic_write_bytes(target, content)


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def evidence_for(batch: Path) -> dict[str, Any]:
    return {
        key: {"manifest_path": str(batch / name), "sha256": file_hash(batch / name)}
        for key, name in (
            ("calculation_status", "status.tsv"),
            ("downloaded_results", "download-results.tsv"),
        )
    }


def worker_ids(report: list[dict[str, str]]) -> set[str]:
    ids = {row.get("worker", "") for row in report}
    if not report or "" in ids or len(ids) != len(report):
        raise InvalidRequestError("batch report must contain unique nonempty worker IDs")
    return ids


@dataclass(frozen=True)
class CampaignRequest:
    campaign_id: str
    batch_run_dir: str
    max_new_batches: int = 1
    max_selected_candidates: int = 100
    poll_seconds: float = 60
    max_failures: int = 3
    agent_command: tuple[str, ...] = ()
    agent_timeout_seconds: float = 1800
    context_paths: tuple[str, ...] = ()
    initial_selected_ids: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CampaignRequest:
        fields: dict[str, Any] = {
            key: tuple(item)
            if key in {"agent_command", "context_paths", "initial_selected_ids"}
            else item
            for key, item in value.items()
        }
        request = cls(**fields)
        if (
            not request.campaign_id
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
                for character in request.campaign_id
            )
            or request.campaign_id in {".", ".."}
        ):
            raise InvalidRequestError("campaign_id must be a safe nonempty identifier")
        for key in ("max_new_batches", "max_selected_candidates", "max_failures"):
            number = getattr(request, key)
            minimum = 0 if key == "max_new_batches" else 1
            if type(number) is not int or number < minimum:
                raise InvalidRequestError(f"{key} must be an integer >= {minimum}")
        for key in ("poll_seconds", "agent_timeout_seconds"):
            number = getattr(request, key)
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(number)
                or number <= 0
            ):
                raise InvalidRequestError(f"{key} must be positive and finite")
        for key in ("agent_command", "context_paths", "initial_selected_ids"):
            if not isinstance(value.get(key, ()), (list, tuple)) or any(
                not isinstance(item, str) or not item for item in getattr(request, key)
            ):
                raise InvalidRequestError(f"{key} must be an array of nonempty strings")
        if not Path(request.batch_run_dir).is_absolute():
            raise InvalidRequestError("batch_run_dir must be absolute")
        if any(not Path(path).is_absolute() for path in request.context_paths):
            raise InvalidRequestError("context_paths must be absolute")
        return request


class CampaignExecutor(Protocol):
    def execute(
        self, capability: str, request: dict[str, Any], operation_dir: Path
    ) -> TaskResult: ...


class CampaignAgent(Protocol):
    def run(self, task_path: Path, response_path: Path, request: CampaignRequest) -> None: ...


class Campaign:
    """One tick advances at most one stage. Waiting consumes no model tokens."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.path = self.directory / "campaign.toml"

    @classmethod
    def start(
        cls, request: CampaignRequest, *, directory: Path | None = None, workdir: Path = Path(".")
    ) -> Campaign:
        campaign = cls(
            directory or timestamped_run_directory(workdir / "runs", request.campaign_id)
        )
        batch = Path(request.batch_run_dir)
        if not (batch / "run.toml").is_file():
            raise InvalidRequestError("campaign starts from an existing submitted batch run")
        cls.check_submission(batch)
        if any(int(row.get("tasks", "0")) <= 0 for row in rows(batch / "submit-plan.tsv")):
            raise InvalidRequestError(
                "adaptive campaigns require nonempty workers; remove empty workers when preparing the batch"
            )
        campaign.directory.mkdir(parents=True, exist_ok=True)
        with campaign.lock():
            if campaign.path.exists():
                raise InvalidRequestError("campaign already exists; use resume")
            contexts = {
                str(index): {"path": path, "sha256": file_hash(Path(path))}
                for index, path in enumerate(request.context_paths)
            }
            dump_toml(
                campaign.path,
                {
                    "schema": "tricycle.campaign/v1",
                    "config": asdict(request),
                    "context": contexts,
                    "state": {
                        "phase": "monitoring",
                        "round_index": 1,
                        "batch_run_dir": str(batch.resolve()),
                        "new_batches": 0,
                        "selected_count": 0,
                        "selected_ids": list(request.initial_selected_ids),
                        "failures": 0,
                        "batch_history": [str(batch.resolve())],
                        "next_check_at": 0.0,
                        "reason": "",
                        "inflight": "",
                    },
                },
            )
        return campaign

    @contextmanager
    def lock(self) -> Iterator[None]:
        with (self.directory / ".campaign.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InvalidRequestError("another executor owns this campaign") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def read(self) -> dict[str, Any]:
        return load_toml(self.path)

    def pause(self) -> None:
        # Can be requested while an agent command holds the execution lock.
        dump_toml(self.directory / "pause.toml", {"requested": True})

    def resume(self) -> dict[str, Any]:
        with self.lock():
            data = self.read()
            state = data["state"]
            if state["phase"] == "blocked":
                state["phase"] = state["blocked_phase"]
            state.update(failures=0, reason="", next_check_at=0.0)
            (self.directory / "pause.toml").unlink(missing_ok=True)
            self.save(data)
            return data

    def save(self, data: dict[str, Any]) -> None:
        data["state"]["updated_at"] = time.time()
        data["state"]["revision"] = data["state"].get("revision", 0) + 1
        dump_toml(self.directory / "history" / f"{data['state']['revision']:08d}.toml", data)
        dump_toml(self.path, data)

    def tick(
        self,
        executor: CampaignExecutor | None = None,
        agent: CampaignAgent | None = None,
        *,
        live: bool = False,
        allow_submit: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        with self.lock():
            data = self.read()
            state = data["state"]
            request = CampaignRequest.from_mapping(data["config"])
            clock = time.time() if now is None else now
            if not live:
                return {**data, "dry_run": True}
            if (self.directory / "pause.toml").exists():
                return {**data, "paused": True}
            if state["phase"] in {"complete", "blocked"} or clock < state["next_check_at"]:
                return data
            if executor is None:
                raise InvalidRequestError("live campaign requires an injected capability executor")
            previous = state["phase"]
            try:
                self.advance(data, request, executor, agent, allow_submit)
                state["failures"] = 0
                state["reason"] = ""
                state["next_check_at"] = (
                    clock + request.poll_seconds if state["phase"] == previous else 0.0
                )
            except Exception as exc:
                # Failed validation must not commit a partially prepared phase
                # transition. Keep the durable submission intent for reconciliation.
                state["phase"] = previous
                state["failures"] += 1
                state["reason"] = f"{type(exc).__name__}: {exc}"
                state["next_check_at"] = clock + request.poll_seconds * min(
                    2 ** (state["failures"] - 1), 16
                )
                if state["failures"] >= request.max_failures or state["inflight"]:
                    state["blocked_phase"] = previous
                    state["phase"] = "blocked"
            self.save(data)
            return data

    def advance(
        self,
        data: dict[str, Any],
        request: CampaignRequest,
        executor: CampaignExecutor,
        agent: CampaignAgent | None,
        allow_submit: bool,
    ) -> None:
        state = data["state"]
        phase = state["phase"]
        batch = Path(state["batch_run_dir"])
        round_dir = self.directory / "rounds" / f"{state['round_index']:04d}"
        round_dir.mkdir(parents=True, exist_ok=True)

        def execute(capability: str, payload: dict[str, Any]) -> TaskResult:
            result = executor.execute(capability, payload, round_dir / phase)
            if result.status not in {TaskStatus.SUCCEEDED, TaskStatus.SUBMITTED}:
                raise InvalidRequestError(f"{capability}: {result.status.value}: {result.message}")
            return result

        if phase == "monitoring":
            execute("bohr.batch.status", {"run_dir": str(batch)})
            submitted = rows(batch / "submit-results.tsv")
            status = rows(batch / "status.tsv")
            if worker_ids(status) != worker_ids(submitted):
                raise InvalidRequestError("status worker coverage differs from submitted batch")
            if any(row.get("result") == "QUERY_FAILED" for row in status):
                raise InvalidRequestError("Bohr worker status query failed")
            if all(
                row.get("terminal") == "yes" and row.get("result") in {"SUCCEEDED", "FAILED"}
                for row in status
            ):
                state["phase"] = "downloading"
        elif phase == "downloading":
            result = execute(
                "bohr.batch.download",
                {
                    "run_dir": str(batch),
                    "resume": True,
                    "continue_on_error": True,
                },
            )
            # The Bohr lifecycle writes downloads/download-results.tsv, while
            # acquisition evidence expects both reports in the batch root.
            # Materialize an exact, atomic copy; retain the original source.
            for artifact in result.outputs:
                source = Path(artifact.location)
                target = batch / "download-results.tsv"
                if source.name == target.name and source.resolve() != target.resolve():
                    freeze_file(source, target, replace=True)
                    dump_toml(
                        round_dir / "download-source.toml",
                        {
                            "path": str(source.resolve()),
                            "sha256": file_hash(source),
                        },
                    )
            evidence = evidence_for(batch)
            if validate_calculation_evidence(evidence) != worker_ids(
                rows(batch / "submit-results.tsv")
            ):
                raise InvalidRequestError("download worker coverage differs from submitted batch")
            dump_toml(round_dir / "evidence.toml", evidence)
            if (
                state["new_batches"] >= request.max_new_batches
                or state["selected_count"] >= request.max_selected_candidates
            ):
                state.update(
                    phase="complete", stop_reason="campaign limit reached; final batch downloaded"
                )
            else:
                state["phase"] = "analyzing"
        elif phase in {"analyzing", "preparing"}:
            evidence = load_toml(round_dir / "evidence.toml")
            validate_calculation_evidence(evidence)
            response = self.agent_response(data, request, round_dir, agent)
            if response is None:
                return
            if phase == "analyzing":
                if response.get("action") == "stop":
                    if not str(response.get("reason", "")).strip():
                        raise InvalidRequestError("agent stop requires a recorded reason")
                    state.update(phase="complete", stop_reason=response["reason"])
                    return
                source = self.verified_path(response, "request")
                parsed = load_toml(source)
                if parsed.get("strategy") != "agent-authored-acquisition":
                    raise InvalidRequestError("campaign requires agent-authored-acquisition")
                if (
                    parsed.get("exploration_id") != request.campaign_id
                    or parsed.get("round_index") != state["round_index"]
                ):
                    raise InvalidRequestError("agent request belongs to a different campaign/round")
                supplied = (
                    parsed.get("metadata", {}).get("agent_acquisition", {}).get("evidence", {})
                )
                if any(supplied.get(key) != value for key, value in evidence.items()):
                    raise InvalidRequestError(
                        "agent request must bind this round's exact calculation evidence"
                    )
                budget = parsed.get("budget", 0)
                if (
                    type(budget) is not int
                    or not 0 < budget <= request.max_selected_candidates - state["selected_count"]
                ):
                    raise InvalidRequestError("selection budget exceeds campaign allowance")
                frozen = round_dir / "selection-request.toml"
                if frozen.exists() and frozen.read_bytes() != source.read_bytes():
                    raise InvalidRequestError("selection request is immutable")
                freeze_file(source, frozen)
                state.update(phase="selecting", request_sha256=file_hash(frozen))
            else:
                selected = Path(state["selection_path"])
                if (
                    file_hash(selected) != state["selection_sha256"]
                    or response.get("selection_sha256") != state["selection_sha256"]
                ):
                    raise InvalidRequestError("prepared batch must bind the frozen selection")
                prepared = Path(str(response.get("batch_run_dir", "")))
                if not prepared.is_absolute() or str(prepared.resolve()) in state["batch_history"]:
                    raise InvalidRequestError("prepare a new independent batch at an absolute path")
                if (prepared / "submit-results.tsv").exists() and any(
                    row.get("job_id") or row.get("status") not in {"PREPARED", "DRY_RUN"}
                    for row in rows(prepared / "submit-results.tsv")
                ):
                    raise InvalidRequestError(
                        "agent must prepare without submitting the next batch"
                    )
                for name in ("run.toml", "batch.toml", "submit-plan.tsv"):
                    if not (prepared / name).is_file():
                        raise InvalidRequestError(f"prepared batch missing {name}")
                if response.get("plan_sha256") != file_hash(prepared / "submit-plan.tsv"):
                    raise InvalidRequestError("prepared batch plan hash differs")
                plan_rows = rows(prepared / "submit-plan.tsv")
                worker_ids(plan_rows)
                if any(int(row.get("tasks", "0")) <= 0 for row in plan_rows):
                    raise InvalidRequestError("adaptive campaigns require nonempty workers")
                ids = [row["candidate_id"] for row in rows(selected)]
                if sorted(response.get("candidate_ids", [])) != sorted(ids):
                    raise InvalidRequestError("prepared batch candidate IDs differ from selection")
                state.update(
                    phase="submitting",
                    next_batch=str(prepared.resolve()),
                    plan_sha256=response["plan_sha256"],
                )
                dump_toml(round_dir / "prepared-inputs.toml", self.prepared_inputs(prepared))
            dump_toml(
                round_dir / phase / "accepted-response.toml",
                {"response_sha256": file_hash(round_dir / phase / "response.toml")},
            )
        elif phase == "selecting":
            frozen = round_dir / "selection-request.toml"
            if file_hash(frozen) != state["request_sha256"]:
                raise InvalidRequestError("frozen selection request was modified")
            parsed = load_toml(frozen)
            parsed.update(
                output_dir=str(round_dir / "selection"),
                resume=True,
            )
            result = execute("exploration.round", parsed)
            selected = Path(str(result.metadata["round_dir"])) / "selected-candidates.tsv"
            ids = [row["candidate_id"] for row in rows(selected)]
            if len(set(ids)) != len(ids) or set(ids) & set(state["selected_ids"]):
                raise InvalidRequestError("selection repeats an already selected candidate")
            if not ids:
                state.update(phase="complete", stop_reason="no candidates selected")
                return
            state.update(
                phase="preparing",
                selection_path=str(selected),
                selection_sha256=file_hash(selected),
            )
        elif phase == "submitting":
            prepared = Path(state["next_batch"])
            if file_hash(prepared / "submit-plan.tsv") != state["plan_sha256"]:
                raise InvalidRequestError("prepared batch plan changed before submission")
            if state["inflight"]:
                # A crash may occur after remote acceptance but before a local receipt.
                # Never blindly call submit again; require complete local reconciliation.
                if not (prepared / "submit-results.tsv").exists():
                    raise InvalidRequestError(
                        "uncertain submission: reconcile remote jobs and submit-results.tsv before resume"
                    )
                self.check_submission(prepared)
            else:
                if not allow_submit:
                    state["awaiting_submission_authorization"] = True
                    return
                if self.prepared_inputs(prepared) != load_toml(round_dir / "prepared-inputs.toml"):
                    raise InvalidRequestError("prepared worker inputs changed before submission")
                if state["new_batches"] >= request.max_new_batches:
                    raise InvalidRequestError("campaign submission limit reached")
                selected = Path(state["selection_path"])
                if file_hash(selected) != state["selection_sha256"]:
                    raise InvalidRequestError("selection changed before submission")
                if state["selected_count"] + len(rows(selected)) > request.max_selected_candidates:
                    raise InvalidRequestError("campaign candidate limit reached")
                state["awaiting_submission_authorization"] = False
                state["inflight"] = str(prepared)
                self.save(data)
                execute("bohr.batch.submit", {"run_dir": str(prepared)})
                self.check_submission(prepared)
            selected = Path(state["selection_path"])
            if file_hash(selected) != state["selection_sha256"]:
                raise InvalidRequestError("selection changed during submission")
            ids = [row["candidate_id"] for row in rows(selected)]
            state.update(
                phase="monitoring",
                batch_run_dir=str(prepared),
                inflight="",
                new_batches=state["new_batches"] + 1,
                selected_count=state["selected_count"] + len(ids),
                batch_history=[*state["batch_history"], str(prepared)],
                selected_ids=[*state["selected_ids"], *ids],
                round_index=state["round_index"] + 1,
            )
        else:
            raise InvalidRequestError(f"unknown campaign phase: {phase}")

    @staticmethod
    def prepared_inputs(batch: Path) -> dict[str, Any]:
        paths = {batch / "batch.toml", batch / "submit-plan.tsv"}
        for row in rows(batch / "submit-plan.tsv"):
            root = Path(row["input_dir"])
            root = root if root.is_absolute() else batch / root
            if not root.is_dir():
                raise InvalidRequestError(f"worker input directory missing: {root}")
            files = {path.resolve() for path in root.rglob("*") if path.is_file()}
            if not files:
                raise InvalidRequestError(f"worker input directory empty: {root}")
            paths.update(files)
        return {
            "files": {
                str(index): {"path": str(path), "sha256": file_hash(path)}
                for index, path in enumerate(sorted(paths))
            }
        }

    @staticmethod
    def check_submission(batch: Path) -> None:
        expected = [
            row for row in rows(batch / "submit-plan.tsv") if int(row.get("tasks", "0")) > 0
        ]
        actual = [
            row for row in rows(batch / "submit-results.tsv") if int(row.get("tasks", "0")) > 0
        ]
        if worker_ids(expected) != worker_ids(actual) or any(
            not row.get("job_id") or row.get("status") not in {"SUBMITTED", "REUSED"}
            for row in actual
        ):
            raise InvalidRequestError(
                "submission incomplete: reconcile all worker receipts before resuming"
            )

    @staticmethod
    def verified_path(response: dict[str, Any], key: str) -> Path:
        path = Path(str(response.get(f"{key}_path", "")))
        if (
            not path.is_absolute()
            or not path.is_file()
            or file_hash(path) != response.get(f"{key}_sha256")
        ):
            raise InvalidRequestError(f"agent {key} path/hash is missing or invalid")
        return path

    def agent_response(
        self,
        data: dict[str, Any],
        request: CampaignRequest,
        round_dir: Path,
        agent: CampaignAgent | None,
    ) -> dict[str, Any] | None:
        state = data["state"]
        task_dir = round_dir / state["phase"]
        task_dir.mkdir(parents=True, exist_ok=True)
        task_path, response_path = task_dir / "task.toml", task_dir / "response.toml"
        if not task_path.exists():
            for artifact in data["context"].values():
                if file_hash(Path(artifact["path"])) != artifact["sha256"]:
                    raise InvalidRequestError("campaign context artifact changed")
            task = {
                "schema": "tricycle.campaign-agent-task/v1",
                "campaign_id": request.campaign_id,
                "campaign_dir": str(self.directory),
                "round_index": state["round_index"],
                "phase": state["phase"],
                "batch_run_dir": state["batch_run_dir"],
                "evidence_path": str(round_dir / "evidence.toml"),
                "response_path": str(response_path),
                "context": data["context"],
                "remaining_candidates_budget": request.max_selected_candidates
                - state["selected_count"],
                "selected_ids": state["selected_ids"],
                "selection_path": state.get("selection_path", "")
                if state["phase"] == "preparing"
                else "",
                "selection_sha256": state.get("selection_sha256", "")
                if state["phase"] == "preparing"
                else "",
                "instructions": "Follow exploration-campaign and agent-authored-acquisition skills. Analyze only after the batch barrier. Do not submit or terminate QM batches. Return an atomic response bound to task_sha256. Record model work separately under explicit authorization.",
            }
            dump_toml(task_path, task)
        if not response_path.exists() and agent is not None:
            marker = task_dir / "agent-invocation.toml"
            if marker.exists():
                raise InvalidRequestError(
                    "agent invocation interrupted; recover its response or explicitly remove agent-invocation.toml after confirming it stopped"
                )
            dump_toml(marker, {"task_sha256": file_hash(task_path), "started_at": time.time()})
            agent.run(task_path, response_path, request)
            if not response_path.exists():
                raise InvalidRequestError("agent exited without response.toml")
        state["agent_task_path"] = str(task_path)
        if not response_path.exists():
            return None
        response = load_toml(response_path)
        if response.get("task_sha256") != file_hash(task_path):
            raise InvalidRequestError("agent response task hash differs")
        receipt = task_dir / "accepted-response.toml"
        if receipt.exists() and load_toml(receipt)["response_sha256"] != file_hash(response_path):
            raise InvalidRequestError("accepted agent response was modified")
        # Mark accepted only after the caller has validated and persisted its transition.
        # The original response stays on disk for replay after a process interruption.
        return response
