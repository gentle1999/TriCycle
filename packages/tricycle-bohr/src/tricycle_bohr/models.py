from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Self

from tricycle_core import Artifact, BohrJobSpec, ModelMode, load_dotenv
from tricycle_core.errors import InvalidRequestError


def _artifact(value: object, *, field_name: str, default_kind: str = "file") -> Artifact | None:
    if value is None or value == "":
        return None
    if isinstance(value, Artifact):
        return value
    if isinstance(value, (str, Path)):
        return Artifact(f"{field_name}-artifact", str(value), kind=default_kind)
    if not isinstance(value, Mapping):
        raise InvalidRequestError(f"{field_name} must be an artifact or path")
    try:
        return Artifact(**value)
    except TypeError as exc:
        raise InvalidRequestError(f"{field_name} must contain artifact_id and location") from exc


def _artifacts(values: object) -> tuple[Artifact, ...]:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        raise InvalidRequestError("artifacts must be a list")
    result: list[Artifact] = []
    for index, value in enumerate(values):
        artifact = _artifact(value, field_name=f"artifact-{index}")
        if artifact is not None:
            result.append(artifact)
    return tuple(result)


def _command(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise InvalidRequestError("command must be a list")
    command = tuple(str(part) for part in value)
    if any(not part.strip() for part in command):
        raise InvalidRequestError("command entries must not be empty")
    return command


def _strings(value: object, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise InvalidRequestError(f"{field_name} must be a list")
    return tuple(str(item) for item in value)


def _env_or(
    payload: Mapping[str, Any], key: str, environment_name: str, default: object = None
) -> Any:
    value = payload.get(key)
    if value is not None and str(value).strip():
        return value
    load_dotenv(keys=(environment_name,))
    return os.environ.get(environment_name, default)


def _parameters(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidRequestError("parameters must be a table")
    return dict(value)


@dataclass(frozen=True, slots=True)
class BohrComputeRequest:
    job_name: str
    operation: str
    image: str
    command: tuple[str, ...] = ()
    project_id: str | None = None
    machine_type: str | None = None
    input_artifacts: tuple[Artifact, ...] = ()
    output_artifacts: tuple[Artifact, ...] = ()
    parameters: Mapping[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()

    def to_job_spec(self) -> BohrJobSpec:
        return BohrJobSpec(
            name=self.job_name,
            operation=self.operation,
            image=self.image,
            command=self.command,
            project_id=self.project_id,
            machine_type=self.machine_type,
            input_artifacts=self.input_artifacts,
            output_artifacts=self.output_artifacts,
            parameters=self.parameters,
            tags=self.tags,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> BohrComputeRequest:
        return cls(
            job_name=str(payload.get("job_name", payload.get("name", ""))),
            operation=str(payload.get("operation", "")),
            image=str(_env_or(payload, "image", "BOHR_IMAGE", "")),
            command=_command(payload.get("command", ())),
            project_id=_env_or(payload, "project_id", "BOHR_PROJECT_ID"),
            machine_type=_env_or(payload, "machine_type", "BOHR_MACHINE_TYPE"),
            input_artifacts=_artifacts(payload.get("input_artifacts")),
            output_artifacts=_artifacts(payload.get("output_artifacts")),
            parameters=_parameters(payload.get("parameters", {})),
            tags=_strings(payload.get("tags", ()), field_name="tags"),
        )


@dataclass(frozen=True, slots=True)
class BohrModelJobRequest:
    """Common request contract for a model task executed by one Bohr job.

    SAR and RitS differ in model semantics, but their platform boundary is
    identical: upload task inputs, run a command in the shared worker image,
    and expose the resulting job through :class:`BohrGateway`.  This class
    owns that common boundary; domain packages only select ``model_family``.
    """

    model_name: str
    mode: ModelMode
    image: str
    dataset: Artifact | None = None
    checkpoint: Artifact | None = None
    code_artifact: Artifact | None = None
    input_artifacts: tuple[Artifact, ...] = ()
    output_artifacts: tuple[Artifact, ...] = ()
    command: tuple[str, ...] = ()
    project_id: str | None = None
    machine_type: str | None = None
    input_directory: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)

    model_family: ClassVar[str] = "model"
    model_label: ClassVar[str] = "model"
    # Some upstream model trainers fine-tune from a checkpoint while loading
    # their dataset/configuration from the checkpoint itself.  Domain
    # requests can opt into the stricter dataset requirement when their
    # upstream protocol needs it (RitS does; RxnResid refit does not).
    fine_tune_requires_dataset: ClassVar[bool] = False

    def __post_init__(self) -> None:
        if not self.model_name.strip():
            raise InvalidRequestError(f"{self.model_label} model_name must not be empty")
        if not self.image.strip():
            raise InvalidRequestError(f"{self.model_label} image must not be empty")
        if self.code_artifact is None:
            raise InvalidRequestError(
                f"{self.model_label} code_artifact is required; upload the task repository"
            )
        if self.input_directory is None or not self.input_directory.strip():
            raise InvalidRequestError(
                f"{self.model_label} input_directory is required for Bohr CLI upload"
            )
        if self.mode is ModelMode.TRAIN and self.dataset is None:
            raise InvalidRequestError(f"{self.model_label} training requires a dataset artifact")
        if self.mode is ModelMode.INFER and self.checkpoint is None:
            raise InvalidRequestError(
                f"{self.model_label} inference requires a checkpoint artifact"
            )
        if self.mode is ModelMode.FINETUNE:
            if self.checkpoint is None:
                raise InvalidRequestError(
                    f"{self.model_label} fine-tuning requires a checkpoint artifact"
                )
            if self.fine_tune_requires_dataset and self.dataset is None:
                raise InvalidRequestError(
                    f"{self.model_label} fine-tuning requires a dataset artifact"
                )
        if not self.command:
            raise InvalidRequestError(
                f"{self.model_label} command is required; provide a bash entrypoint "
                "in input_directory to install and run the task repository"
            )
        if any(not part.strip() for part in self.command):
            raise InvalidRequestError("command entries must not be empty")

    @property
    def operation(self) -> str:
        return f"{self.model_family}.{self.mode.value}"

    @property
    def job_name(self) -> str:
        return f"{self.model_family}-{self.mode.value}-{self.model_name}"

    @property
    def all_input_artifacts(self) -> tuple[Artifact, ...]:
        required = tuple(
            artifact
            for artifact in (self.code_artifact, self.dataset, self.checkpoint)
            if artifact is not None
        )
        return required + self.input_artifacts

    def to_bohr_job(self) -> BohrJobSpec:
        parameters = dict(self.parameters)
        if self.input_directory is not None:
            parameters["input_directory"] = self.input_directory
        parameters.update({"model_name": self.model_name, "mode": self.mode.value})
        return BohrJobSpec(
            name=self.job_name,
            operation=self.operation,
            image=self.image,
            command=self.command,
            project_id=self.project_id,
            machine_type=self.machine_type,
            input_artifacts=self.all_input_artifacts,
            output_artifacts=self.output_artifacts,
            parameters=parameters,
            tags=("tricycle", self.model_family, self.mode.value),
        )

    @classmethod
    def from_mapping(cls: type[Self], payload: Mapping[str, Any], mode: ModelMode) -> Self:
        code_value = payload.get(
            "code_artifact",
            payload.get("code", payload.get("source_artifact", payload.get("repository"))),
        )
        input_directory = payload.get("input_directory", payload.get("input_dir"))
        return cls(
            model_name=str(payload.get("model_name", "")),
            mode=mode,
            image=str(_env_or(payload, "image", "BOHR_ML_IMAGE", "")),
            dataset=_artifact(payload.get("dataset"), field_name="dataset"),
            checkpoint=_artifact(payload.get("checkpoint"), field_name="checkpoint"),
            code_artifact=_artifact(
                code_value,
                field_name="code",
                default_kind="directory",
            ),
            input_artifacts=_artifacts(payload.get("input_artifacts")),
            output_artifacts=_artifacts(payload.get("output_artifacts")),
            command=_command(payload.get("command", ())),
            project_id=_env_or(payload, "project_id", "BOHR_PROJECT_ID"),
            machine_type=_env_or(payload, "machine_type", "BOHR_MACHINE_TYPE"),
            input_directory=str(input_directory) if input_directory is not None else None,
            parameters=_parameters(payload.get("parameters", {})),
        )
