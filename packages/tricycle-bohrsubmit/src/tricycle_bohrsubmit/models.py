from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tricycle_core import load_dotenv
from tricycle_core.errors import InvalidRequestError

DEFAULT_CHEMISTRY_IMAGE = (
    "registry.dp.tech/dptech/dp/native/prod-23278/"
    "gaussian-orca-dp-client:260824"
)


def _paths(value: object) -> tuple[Path, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, Path)):
        return (Path(value),)
    if not isinstance(value, (list, tuple)):
        raise InvalidRequestError("paths must be a string or list")
    return tuple(Path(str(item)) for item in value)


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple)):
        raise InvalidRequestError("values must be a string or list")
    return tuple(str(item) for item in value)


def _optional_path(value: object) -> Path | None:
    if value is None or value == "":
        return None
    return Path(str(value))


def _env_or(
    payload: Mapping[str, Any], key: str, environment_name: str, default: object = None
) -> Any:
    value = payload.get(key)
    if value is not None and str(value).strip():
        return value
    load_dotenv(keys=(environment_name,))
    return os.environ.get(environment_name, default)


@dataclass(frozen=True, slots=True)
class BatchRequest:
    """Description of the generic file-oriented Bohr batch workflow.

    This is the typed equivalent of ``bohr-batch-submit``'s command-line
    arguments.  It deliberately contains paths and command arguments, but no
    credentials or live gateway object.
    """

    task_patterns: tuple[str, ...]
    workers: int
    image: str = DEFAULT_CHEMISTRY_IMAGE
    script: Path | None = None
    worker_script: Path | None = None
    project_id: str | None = None
    machine_type: str | None = None
    common_paths: tuple[Path, ...] = ()
    out_dir: Path | None = None
    job_prefix: str | None = None
    job_group_id: str | None = None
    job_group_name: str | None = None
    auto_job_group: bool = True
    result_path: str | None = None
    dataset_paths: tuple[str, ...] = ()
    backward_files: str = "worker.log,results,manifest.txt,manifest.tsv"
    log_file: str = "worker.log"
    max_reschedule_times: str | None = None
    max_run_time: str | None = None
    nnode: str | None = None
    extra_submit_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.task_patterns:
            raise InvalidRequestError("at least one task pattern is required")
        if self.workers < 1:
            raise InvalidRequestError("workers must be positive")
        if not self.image.strip():
            raise InvalidRequestError("image must not be empty")
        if self.script is None and self.worker_script is None:
            raise InvalidRequestError("script or worker_script is required")
        if self.script is not None and self.worker_script is not None:
            raise InvalidRequestError("script and worker_script are mutually exclusive")
        if not self.project_id or not str(self.project_id).strip():
            raise InvalidRequestError("project_id is required")
        if not self.machine_type or not str(self.machine_type).strip():
            raise InvalidRequestError("machine_type is required")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> BatchRequest:
        task_patterns = payload.get("task_patterns", payload.get("tasks", ()))
        patterns = _strings(task_patterns)
        return cls(
            task_patterns=patterns,
            workers=int(payload.get("workers", 0)),
            image=str(_env_or(payload, "image", "BOHR_IMAGE", DEFAULT_CHEMISTRY_IMAGE)),
            script=_optional_path(payload.get("script")),
            worker_script=_optional_path(payload.get("worker_script")),
            project_id=_env_or(payload, "project_id", "BOHR_PROJECT_ID"),
            machine_type=_env_or(payload, "machine_type", "BOHR_MACHINE_TYPE"),
            common_paths=_paths(payload.get("common_paths", payload.get("common"))),
            out_dir=_optional_path(payload.get("out_dir", payload.get("out"))),
            job_prefix=payload.get("job_prefix"),
            job_group_id=payload.get("job_group_id"),
            job_group_name=payload.get("job_group_name"),
            auto_job_group=bool(payload.get("auto_job_group", True)),
            result_path=payload.get("result_path"),
            dataset_paths=_strings(payload.get("dataset_paths", payload.get("dataset_path"))),
            backward_files=str(
                payload.get("backward_files", "worker.log,results,manifest.txt,manifest.tsv")
            ),
            log_file=str(payload.get("log_file", "worker.log")),
            max_reschedule_times=payload.get("max_reschedule_times"),
            max_run_time=payload.get("max_run_time"),
            nnode=payload.get("nnode"),
            extra_submit_args=_strings(payload.get("extra_submit_args", payload.get("bohr_args"))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_patterns": list(self.task_patterns),
            "workers": self.workers,
            "image": self.image,
            "script": str(self.script) if self.script is not None else None,
            "worker_script": str(self.worker_script) if self.worker_script is not None else None,
            "project_id": self.project_id,
            "machine_type": self.machine_type,
            "common_paths": [str(path) for path in self.common_paths],
            "out_dir": str(self.out_dir) if self.out_dir is not None else None,
            "job_prefix": self.job_prefix,
            "job_group_id": self.job_group_id,
            "job_group_name": self.job_group_name,
            "auto_job_group": self.auto_job_group,
            "result_path": self.result_path,
            "dataset_paths": list(self.dataset_paths),
            "backward_files": self.backward_files,
            "log_file": self.log_file,
            "max_reschedule_times": self.max_reschedule_times,
            "max_run_time": self.max_run_time,
            "nnode": self.nnode,
            "extra_submit_args": list(self.extra_submit_args),
        }


@dataclass(frozen=True, slots=True)
class DpaBernyRequest:
    """Input contract for the migrated DPA-Berny/Gaussian preparer.

    ``engine=orca`` is intentionally not accepted here: the old project did
    not define an ORCA input contract.  ORCA jobs can use ``BatchRequest``
    with an explicit task script until that contract is specified.
    """

    xyz_patterns: tuple[str, ...]
    workers: int
    engine: str = "dpa"
    model_path: Path | None = None
    image: str = DEFAULT_CHEMISTRY_IMAGE
    project_id: str | None = None
    machine_type: str | None = None
    task_type: str = "ts"
    charge: str = "0"
    multiplicity: str = "1"
    nproc: str = "1"
    mem: str = "32GB"
    route: str | None = None
    opt_route: str | None = None
    fixed_route: str | None = None
    functional: str | None = None
    basis: str | None = None
    fixed_only: bool = False
    multi_stage_chk: bool = False
    ignore_constraints: bool = False
    one_task_per_worker: bool = False
    method: str = "dpa2"
    model_name: str = "model.ckpt.pt"
    frozen_model: str = "frozen_model.pth"
    no_freeze: bool = False
    allow_task_failures: bool = False
    server_script: str = (
        "/opt/deepmd-kit-3.1.0/lib/python3.12/site-packages/dihscan/pot/server.py"
    )
    server_wait: str = "20"
    server_health_timeout: str = "120"
    out_dir: Path | None = None
    job_prefix: str | None = None
    job_group_id: str | None = None
    job_group_name: str | None = None
    auto_job_group: bool = True
    result_path: str | None = None
    backward_files: str = "worker.log,server.log,*.log,*.out,*.xyz,*.chk,results"
    log_file: str = "worker.log"
    max_reschedule_times: str | None = None
    max_run_time: str | None = None
    nnode: str | None = None
    extra_submit_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.xyz_patterns:
            raise InvalidRequestError("at least one xyz pattern is required")
        if self.workers < 1:
            raise InvalidRequestError("workers must be positive")
        if self.engine not in {"dpa", "gaussian"}:
            raise InvalidRequestError("engine must be dpa or gaussian")
        if self.task_type not in {"ts", "opt", "auto"}:
            raise InvalidRequestError("task_type must be ts, opt, or auto")
        if not self.image.strip():
            raise InvalidRequestError("image must not be empty")
        if not self.project_id or not str(self.project_id).strip():
            raise InvalidRequestError("project_id is required")
        if not self.machine_type or not str(self.machine_type).strip():
            raise InvalidRequestError("machine_type is required")
        if self.engine == "dpa" and self.model_path is None:
            raise InvalidRequestError("model_path is required for engine=dpa")
        if self.engine == "gaussian":
            if not self.functional or not self.basis:
                raise InvalidRequestError(
                    "functional and basis are required for engine=gaussian"
                )
            if self.model_path is not None:
                raise InvalidRequestError("model_path is only supported for engine=dpa")
        elif self.functional is not None or self.basis is not None:
            raise InvalidRequestError("functional and basis are only supported for engine=gaussian")
        if self.one_task_per_worker and self.workers < 1:
            raise InvalidRequestError("workers must be positive")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> DpaBernyRequest:
        return cls(
            xyz_patterns=_strings(payload.get("xyz_patterns", payload.get("xyz", ()))),
            workers=int(payload.get("workers", 0)),
            engine=str(payload.get("engine", "dpa")),
            model_path=_optional_path(payload.get("model_path", payload.get("model"))),
            image=str(_env_or(payload, "image", "BOHR_IMAGE", DEFAULT_CHEMISTRY_IMAGE)),
            project_id=_env_or(payload, "project_id", "BOHR_PROJECT_ID"),
            machine_type=_env_or(payload, "machine_type", "BOHR_MACHINE_TYPE"),
            task_type=str(payload.get("task_type", "ts")),
            charge=str(payload.get("charge", "0")),
            multiplicity=str(payload.get("multiplicity", "1")),
            nproc=str(payload.get("nproc", "1")),
            mem=str(payload.get("mem", "32GB")),
            route=payload.get("route"),
            opt_route=payload.get("opt_route"),
            fixed_route=payload.get("fixed_route"),
            functional=payload.get("functional"),
            basis=payload.get("basis"),
            fixed_only=bool(payload.get("fixed_only", False)),
            multi_stage_chk=bool(payload.get("multi_stage_chk", False)),
            ignore_constraints=bool(payload.get("ignore_constraints", False)),
            one_task_per_worker=bool(payload.get("one_task_per_worker", False)),
            method=str(payload.get("method", "dpa2")),
            model_name=str(payload.get("model_name", "model.ckpt.pt")),
            frozen_model=str(payload.get("frozen_model", "frozen_model.pth")),
            no_freeze=bool(payload.get("no_freeze", False)),
            allow_task_failures=bool(payload.get("allow_task_failures", False)),
            server_script=str(
                payload.get(
                    "server_script",
                    "/opt/deepmd-kit-3.1.0/lib/python3.12/site-packages/dihscan/pot/server.py",
                )
            ),
            server_wait=str(payload.get("server_wait", "20")),
            server_health_timeout=str(payload.get("server_health_timeout", "120")),
            out_dir=_optional_path(payload.get("out_dir", payload.get("out"))),
            job_prefix=payload.get("job_prefix"),
            job_group_id=payload.get("job_group_id"),
            job_group_name=payload.get("job_group_name"),
            auto_job_group=bool(payload.get("auto_job_group", True)),
            result_path=payload.get("result_path"),
            backward_files=str(
                payload.get(
                    "backward_files",
                    "worker.log,server.log,*.log,*.out,*.xyz,*.chk,results",
                )
            ),
            log_file=str(payload.get("log_file", "worker.log")),
            max_reschedule_times=payload.get("max_reschedule_times"),
            max_run_time=payload.get("max_run_time"),
            nnode=payload.get("nnode"),
            extra_submit_args=_strings(payload.get("extra_submit_args", payload.get("bohr_args"))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "xyz_patterns": list(self.xyz_patterns),
            "workers": self.workers,
            "engine": self.engine,
            "model_path": str(self.model_path) if self.model_path is not None else None,
            "image": self.image,
            "project_id": self.project_id,
            "machine_type": self.machine_type,
            "task_type": self.task_type,
            "charge": self.charge,
            "multiplicity": self.multiplicity,
            "nproc": self.nproc,
            "mem": self.mem,
            "route": self.route,
            "opt_route": self.opt_route,
            "fixed_route": self.fixed_route,
            "functional": self.functional,
            "basis": self.basis,
            "fixed_only": self.fixed_only,
            "multi_stage_chk": self.multi_stage_chk,
            "ignore_constraints": self.ignore_constraints,
            "one_task_per_worker": self.one_task_per_worker,
            "method": self.method,
            "model_name": self.model_name,
            "frozen_model": self.frozen_model,
            "no_freeze": self.no_freeze,
            "allow_task_failures": self.allow_task_failures,
            "server_script": self.server_script,
            "server_wait": self.server_wait,
            "server_health_timeout": self.server_health_timeout,
            "out_dir": str(self.out_dir) if self.out_dir is not None else None,
            "job_prefix": self.job_prefix,
            "job_group_id": self.job_group_id,
            "job_group_name": self.job_group_name,
            "auto_job_group": self.auto_job_group,
            "result_path": self.result_path,
            "backward_files": self.backward_files,
            "log_file": self.log_file,
            "max_reschedule_times": self.max_reschedule_times,
            "max_run_time": self.max_run_time,
            "nnode": self.nnode,
            "extra_submit_args": list(self.extra_submit_args),
        }


@dataclass(frozen=True, slots=True)
class RescueRequest:
    """Requeue policy for a downloaded DPA-Berny/Gaussian run."""

    source_run: Path
    workers: int
    downloads: Path | None = None
    out_dir: Path | None = None
    job_prefix: str | None = None
    job_group_id: str | None = None
    job_group_name: str | None = None
    auto_job_group: bool = True
    allow_missing_downloads: bool = False
    retry_failed_tasks: bool = False

    def __post_init__(self) -> None:
        if not self.source_run:
            raise InvalidRequestError("source_run is required")
        if self.workers < 1:
            raise InvalidRequestError("workers must be positive")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> RescueRequest:
        source = payload.get("source_run", payload.get("rescue_from", payload.get("run", "")))
        return cls(
            source_run=Path(str(source)),
            workers=int(payload.get("workers", 0)),
            downloads=_optional_path(payload.get("downloads")),
            out_dir=_optional_path(payload.get("out_dir", payload.get("out"))),
            job_prefix=payload.get("job_prefix"),
            job_group_id=payload.get("job_group_id"),
            job_group_name=payload.get("job_group_name"),
            auto_job_group=bool(payload.get("auto_job_group", True)),
            allow_missing_downloads=bool(payload.get("allow_missing_downloads", False)),
            retry_failed_tasks=bool(payload.get("retry_failed_tasks", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_run": str(self.source_run),
            "workers": self.workers,
            "downloads": str(self.downloads) if self.downloads is not None else None,
            "out_dir": str(self.out_dir) if self.out_dir is not None else None,
            "job_prefix": self.job_prefix,
            "job_group_id": self.job_group_id,
            "job_group_name": self.job_group_name,
            "auto_job_group": self.auto_job_group,
            "allow_missing_downloads": self.allow_missing_downloads,
            "retry_failed_tasks": self.retry_failed_tasks,
        }
