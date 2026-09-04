from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import InvalidRequestError
from .models import TaskResult
from .toml import dump_toml, load_toml

RUN_SCHEMA = "tricycle.run/v1"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _status_value(result: TaskResult) -> str:
    return result.status.value


class RunStore:
    """Atomic TOML-backed state for a long-running capability invocation."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir.resolve()
        self.path = self.run_dir / "run.toml"

    @classmethod
    def create(
        cls,
        run_dir: Path,
        *,
        run_id: str,
        capability_id: str,
        dry_run: bool,
    ) -> RunStore:
        if not run_id.strip():
            raise InvalidRequestError("run_id must not be empty")
        if not capability_id.strip():
            raise InvalidRequestError("capability_id must not be empty")
        store = cls(run_dir)
        if store.path.exists():
            raise InvalidRequestError(f"run state already exists: {store.path}")
        timestamp = _now()
        store.write(
            {
                "schema": RUN_SCHEMA,
                "run": {
                    "id": run_id,
                    "capability": capability_id,
                    "status": "planned",
                    "phase": "plan",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
                "execution": {
                    "dry_run": dry_run,
                    "attempt": 0,
                    "last_action": "",
                    "last_action_at": "",
                },
                "resume": {
                    "next_action": capability_id,
                    "last_successful_action": "",
                },
                "artifacts": {"run_directory": str(store.run_dir)},
            }
        )
        return store

    @classmethod
    def open(cls, run_dir: Path) -> RunStore:
        store = cls(run_dir)
        data = store.read()
        if data.get("schema") != RUN_SCHEMA:
            raise InvalidRequestError(f"unsupported run state schema: {store.path}")
        run = data.get("run")
        if not isinstance(run, dict) or not str(run.get("id", "")).strip():
            raise InvalidRequestError(f"run state has no valid [run].id: {store.path}")
        return store

    def read(self) -> dict[str, Any]:
        return load_toml(self.path)

    def write(self, data: dict[str, Any]) -> None:
        dump_toml(self.path, data)

    def update(
        self,
        *,
        status: str | None = None,
        phase: str | None = None,
        next_action: str | None = None,
        last_successful_action: str | None = None,
        result: TaskResult | None = None,
        attempt: int | None = None,
        action: str | None = None,
        **sections: dict[str, Any],
    ) -> dict[str, Any]:
        data = self.read()
        run = data.setdefault("run", {})
        if not isinstance(run, dict):
            raise InvalidRequestError(f"run.toml [run] must be a table: {self.path}")
        run["updated_at"] = _now()
        if status is not None:
            run["status"] = status
        if phase is not None:
            run["phase"] = phase

        execution = data.setdefault("execution", {})
        if not isinstance(execution, dict):
            raise InvalidRequestError(f"run.toml [execution] must be a table: {self.path}")
        if attempt is not None:
            execution["attempt"] = attempt
        if action is not None:
            execution["last_action"] = action
            execution["last_action_at"] = run["updated_at"]

        resume = data.setdefault("resume", {})
        if not isinstance(resume, dict):
            raise InvalidRequestError(f"run.toml [resume] must be a table: {self.path}")
        if next_action is not None:
            resume["next_action"] = next_action
        if last_successful_action is not None:
            resume["last_successful_action"] = last_successful_action

        if result is not None:
            result_data = result.to_dict()
            data["result"] = {
                "status": _status_value(result),
                "task_id": result.task_id,
                "plugin_id": result.plugin_id,
                "external_id": result.external_id or "",
                "message": result.message or "",
            }
            if result_data.get("metadata"):
                data["result"]["metadata"] = {
                    key: str(value) for key, value in result_data["metadata"].items()
                }

        for section_name, values in sections.items():
            current = data.setdefault(section_name, {})
            if not isinstance(current, dict):
                raise InvalidRequestError(f"run.toml [{section_name}] must be a table: {self.path}")
            current.update(values)
        self.write(data)
        return data
