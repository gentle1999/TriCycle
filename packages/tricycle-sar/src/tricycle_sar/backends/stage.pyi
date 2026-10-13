"""Static interface for the accompanying runtime implementation."""

from __future__ import annotations

from typing import Any

def run_stage(
    command: Any,
    *,
    name: Any,
    output: Any,
    timeout: Any,
    accelerator: Any,
    progress_timeout: Any = ...,
    evidence: Any = ...,
) -> None: ...
def main() -> None: ...
