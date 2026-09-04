from __future__ import annotations

from pathlib import Path

from tricycle_core import BohrJobSpec


class DryRunBohrGateway:
    """Deterministic in-memory gateway used by tests and local planning demos."""

    def __init__(self) -> None:
        self.submitted: dict[str, BohrJobSpec] = {}

    def submit(self, job: BohrJobSpec) -> str:
        external_id = f"dry-run/{job.name}"
        self.submitted[external_id] = job
        return external_id

    def status(self, external_id: str) -> dict[str, object]:
        if external_id not in self.submitted:
            return {"external_id": external_id, "status": "unknown"}
        return {"external_id": external_id, "status": "planned"}

    def download(self, external_id: str, destination: Path) -> tuple[str, ...]:
        del destination
        if external_id not in self.submitted:
            return ()
        return ()
