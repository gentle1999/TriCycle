"""Common source and protocol binding for model tasks."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tricycle_core import ModelMode, TaskPlan
from tricycle_core.errors import InvalidRequestError


def bind_model_source(
    plan: TaskPlan,
    *,
    mode: ModelMode,
    label: str,
    repository: str,
    revision: str,
    training_entrypoint: str,
    inference_entrypoint: str,
    inference_protocol: str,
    finetune_protocol: str,
) -> TaskPlan:
    job = dict(plan.parameters["bohr_job"])
    parameters = dict(job["parameters"])
    expected = {
        "source_repository": repository,
        "source_revision": revision,
        "upstream_entrypoint": inference_entrypoint
        if mode.value == "infer"
        else training_entrypoint,
        "upstream_protocol": {
            "train": "train",
            "infer": inference_protocol,
            "finetune": finetune_protocol,
        }[mode.value],
    }
    for key, value in expected.items():
        supplied = parameters.get(key)
        if supplied is not None and (not isinstance(supplied, str) or supplied.strip() != value):
            raise InvalidRequestError(
                f"{label} {key} must be {value!r} for the configured upstream repository"
            )
        parameters[key] = value
    job["parameters"] = parameters
    return replace(
        plan,
        parameters={**plan.parameters, "bohr_job": job},
        metadata={**plan.metadata, "source_repository": repository, "source_revision": revision},
    )


def validate_source_checkout(source: str | Path, revision: str, *, include_untracked: bool = True) -> None:
    """Validate a local model source without fetching or changing its checkout."""
    import subprocess

    source = Path(source).resolve()
    actual = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != revision:
        raise ValueError("Model source revision mismatch")
    command = ["git", "-C", str(source), "status", "--porcelain"]
    if not include_untracked:
        command.append("--untracked-files=no")
    if subprocess.check_output(command, text=True).strip():
        raise ValueError("Model source checkout must be clean")
