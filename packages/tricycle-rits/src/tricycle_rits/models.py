from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from tricycle_bohr import BohrModelJobRequest


@dataclass(frozen=True, slots=True)
class RitsJobRequest(BohrModelJobRequest):
    """RitS-specific name for the shared Bohr model job request."""

    model_family: ClassVar[str] = "rits"
    model_label: ClassVar[str] = "RitS"
    fine_tune_requires_dataset: ClassVar[bool] = True
