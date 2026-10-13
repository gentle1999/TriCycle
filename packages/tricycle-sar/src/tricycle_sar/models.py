from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from tricycle_bohr import BohrModelJobRequest


@dataclass(frozen=True, slots=True)
class RxnResidJobRequest(BohrModelJobRequest):
    """RxnResid request using the stable ``sar.*`` capability namespace."""

    model_family: ClassVar[str] = "sar"
    model_label: ClassVar[str] = "RxnResid"
