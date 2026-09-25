from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from tricycle_core.errors import IntegrationNotConfiguredError, InvalidRequestError

_HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_USER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_CONTEXT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_REMOTE_ROOT_PATTERN = re.compile(r"^/[A-Za-z0-9._/+@-]+$")
_REMOTE_RELATIVE_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]*$")
_GPU_SELECTION_PATTERN = re.compile(r"^(?:all|[0-9]+(?:,[0-9]+)*)$")


class RitsSshError(RuntimeError):
    """Raised when Docker context setup or a remote task operation fails."""


@dataclass(frozen=True, slots=True)
class RitsSshFinetuneRequest:
    """Inputs for one RitS job managed through an SSH-backed Docker context.

    SSH authentication is intentionally not part of this request. OpenSSH
    configuration or a loaded agent is used by scp and Docker's SSH context
    transport; planning never inspects either one.
    """

    host: str
    remote_root: str
    bundle_dir: str | None = None
    command: tuple[str, ...] = ()
    container_image: str | None = None
    container_gpus: str = "all"
    container_workdir: str = "/workspace/job"
    docker_context: str = "remote"
    user: str | None = None
    port: int = 22
    connect_timeout: int = 10
    transfer_timeout: int = 3600
    job_id: str | None = None
    log_lines: int = 200
    download_paths: tuple[str, ...] = ()
    download_dir: str | None = None

    def __post_init__(self) -> None:
        if not _HOST_PATTERN.fullmatch(self.host) or self.host.startswith("-"):
            raise InvalidRequestError("host must be an SSH alias or hostname without whitespace")
        if self.user is not None and not _USER_PATTERN.fullmatch(self.user):
            raise InvalidRequestError("user must be a valid SSH username")
        if not _REMOTE_ROOT_PATTERN.fullmatch(self.remote_root):
            raise InvalidRequestError(
                "remote_root must be an absolute path without spaces or shell metacharacters"
            )
        if any(part in {".", ".."} for part in PurePosixPath(self.remote_root).parts):
            raise InvalidRequestError("remote_root must not contain '.' or '..' path components")
        if self.remote_root == "/":
            raise InvalidRequestError("remote_root must not be the filesystem root")
        if not _CONTEXT_PATTERN.fullmatch(self.docker_context):
            raise InvalidRequestError("docker_context must be a valid Docker context name")
        if not 1 <= self.port <= 65535:
            raise InvalidRequestError("port must be between 1 and 65535")
        if not 1 <= self.connect_timeout <= 120:
            raise InvalidRequestError("connect_timeout must be between 1 and 120 seconds")
        if self.transfer_timeout < 1:
            raise InvalidRequestError("transfer_timeout must be positive")
        if self.job_id is not None and not _JOB_ID_PATTERN.fullmatch(self.job_id):
            raise InvalidRequestError("job_id contains unsupported characters")
        if self.bundle_dir is not None and not self.bundle_dir.strip():
            raise InvalidRequestError("bundle_dir must not be empty when provided")
        if self.container_image is not None and not _IMAGE_PATTERN.fullmatch(self.container_image):
            raise InvalidRequestError("container_image must be a Docker image reference")
        if not _GPU_SELECTION_PATTERN.fullmatch(self.container_gpus):
            raise InvalidRequestError("container_gpus must be 'all' or comma-separated GPU indexes")
        if (
            not self.container_workdir.startswith("/")
            or not _REMOTE_RELATIVE_PATTERN.fullmatch(self.container_workdir.lstrip("/"))
            or any(part in {".", ".."} for part in PurePosixPath(self.container_workdir).parts)
        ):
            raise InvalidRequestError("container_workdir must be an absolute safe POSIX path")
        if any(not value.strip() or "\x00" in value for value in self.command):
            raise InvalidRequestError("command entries must be non-empty and contain no NUL bytes")
        if not 1 <= self.log_lines <= 5000:
            raise InvalidRequestError("log_lines must be between 1 and 5000")
        for value in self.download_paths:
            if (
                not _REMOTE_RELATIVE_PATTERN.fullmatch(value)
                or value.startswith("/")
                or any(part in {".", ".."} for part in PurePosixPath(value).parts)
            ):
                raise InvalidRequestError(
                    "download_paths must be relative paths without shell metacharacters"
                )
        if self.download_dir is not None and not self.download_dir.strip():
            raise InvalidRequestError("download_dir must not be empty when provided")

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def docker_endpoint(self) -> str:
        endpoint = f"ssh://{self.target}"
        # Omitting the default port lets an SSH config alias keep its configured port.
        return endpoint if self.port == 22 else f"{endpoint}:{self.port}"

    def remote_job_dir(self, job_id: str | None = None) -> str:
        identifier = job_id or self.job_id
        if identifier is None or not _JOB_ID_PATTERN.fullmatch(identifier):
            raise InvalidRequestError("a valid job_id is required for remote task operations")
        return f"{self.remote_root.rstrip('/')}/{identifier}"

    def container_name(self, job_id: str | None = None) -> str:
        identifier = job_id or self.job_id
        if identifier is None or not _JOB_ID_PATTERN.fullmatch(identifier):
            raise InvalidRequestError("a valid job_id is required for remote task operations")
        return f"tricycle-rits-{identifier[:96]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "remote_root": self.remote_root,
            "bundle_dir": self.bundle_dir,
            "command": list(self.command),
            "container_image": self.container_image,
            "container_gpus": self.container_gpus,
            "container_workdir": self.container_workdir,
            "docker_context": self.docker_context,
            "user": self.user,
            "port": self.port,
            "connect_timeout": self.connect_timeout,
            "transfer_timeout": self.transfer_timeout,
            "job_id": self.job_id,
            "log_lines": self.log_lines,
            "download_paths": list(self.download_paths),
            "download_dir": self.download_dir,
        }

    @classmethod
    def from_mapping(
        cls, payload: dict[str, Any], *, operation: str
    ) -> RitsSshFinetuneRequest:
        if {"password", "private_key", "identity_file"}.intersection(payload):
            raise InvalidRequestError(
                "SSH secrets and identity_file are not request fields; configure OpenSSH agent/config"
            )
        raw_command = payload.get("command", ())
        if isinstance(raw_command, str) or not isinstance(raw_command, (list, tuple)):
            raise InvalidRequestError("command must be a list of strings")
        command = tuple(str(part) for part in raw_command)

        raw_paths = payload.get("download_paths", ())
        if isinstance(raw_paths, str) or not isinstance(raw_paths, (list, tuple)):
            raise InvalidRequestError("download_paths must be a list of relative paths")
        download_paths = tuple(str(part) for part in raw_paths)

        try:
            port = int(payload.get("port", 22))
            connect_timeout = int(payload.get("connect_timeout", 10))
            transfer_timeout = int(payload.get("transfer_timeout", 3600))
            log_lines = int(payload.get("log_lines", 200))
        except (TypeError, ValueError) as exc:
            raise InvalidRequestError("SSH port and timeout/log values must be integers") from exc

        request = cls(
            host=str(payload.get("host", "")),
            remote_root=str(payload.get("remote_root", "")),
            bundle_dir=(str(payload["bundle_dir"]) if payload.get("bundle_dir") is not None else None),
            command=command,
            container_image=(
                str(payload["container_image"]) if payload.get("container_image") else None
            ),
            container_gpus=str(payload.get("container_gpus", "all")),
            container_workdir=str(payload.get("container_workdir", "/workspace/job")),
            docker_context=str(payload.get("docker_context", "remote")),
            user=(str(payload["user"]) if payload.get("user") else None),
            port=port,
            connect_timeout=connect_timeout,
            transfer_timeout=transfer_timeout,
            job_id=(str(payload["job_id"]) if payload.get("job_id") else None),
            log_lines=log_lines,
            download_paths=download_paths,
            download_dir=(str(payload["download_dir"]) if payload.get("download_dir") else None),
        )
        if operation == "submit":
            if not request.bundle_dir:
                raise InvalidRequestError("bundle_dir is required for SSH fine-tuning")
            if not request.command:
                raise InvalidRequestError("command is required for SSH fine-tuning")
            if not request.container_image:
                raise InvalidRequestError("container_image is required for Docker fine-tuning")
        elif operation in {"status", "logs", "terminate", "download"} and not request.job_id:
            raise InvalidRequestError(f"job_id is required for SSH {operation}")
        if operation == "download":
            if not request.download_paths:
                raise InvalidRequestError("download_paths is required for SSH download")
            if not request.download_dir:
                raise InvalidRequestError("download_dir is required for SSH download")
        return request


class RitsSshGateway(Protocol):
    """Remote task lifecycle protocol implemented by injected Docker context adapters."""

    def submit(self, request: RitsSshFinetuneRequest, job_id: str) -> dict[str, Any]: ...

    def status(self, request: RitsSshFinetuneRequest, job_id: str) -> dict[str, Any]: ...

    def logs(self, request: RitsSshFinetuneRequest, job_id: str) -> str: ...

    def terminate(self, request: RitsSshFinetuneRequest, job_id: str) -> dict[str, Any]: ...

    def download(self, request: RitsSshFinetuneRequest, job_id: str) -> tuple[str, ...]: ...


@dataclass
class InMemoryRitsSshGateway:
    """Deterministic adapter for tests and local protocol-level dry runs."""

    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    log_data: dict[str, str] = field(default_factory=dict)
    terminated: list[str] = field(default_factory=list)

    def submit(self, request: RitsSshFinetuneRequest, job_id: str) -> dict[str, Any]:
        if job_id in self.jobs:
            raise RitsSshError(f"Docker context job already exists: {job_id}")
        result = {
            "job_id": job_id,
            "state": "running",
            "remote_dir": request.remote_job_dir(job_id),
            "docker_context": request.docker_context,
            "container_runtime": "docker",
            "container_image": request.container_image,
        }
        self.jobs[job_id] = result
        return dict(result)

    def status(self, request: RitsSshFinetuneRequest, job_id: str) -> dict[str, Any]:
        return dict(self.jobs.get(job_id, {"job_id": job_id, "state": "missing"}))

    def logs(self, request: RitsSshFinetuneRequest, job_id: str) -> str:
        return self.log_data.get(job_id, "")

    def terminate(self, request: RitsSshFinetuneRequest, job_id: str) -> dict[str, Any]:
        self.terminated.append(job_id)
        result = {"job_id": job_id, "state": "stopped"}
        self.jobs[job_id] = result
        return dict(result)

    def download(self, request: RitsSshFinetuneRequest, job_id: str) -> tuple[str, ...]:
        return tuple(f"{request.download_dir}/{value}" for value in request.download_paths)


class OpenSshRitsGateway:
    """Stage task files over SSH and manage a remote container via Docker context.

    On submit, this adapter idempotently creates or validates the configured
    local Docker context (default name: remote), stages the bundle with scp,
    and launches the detached training container using docker --context.
    """

    def __init__(
        self,
        *,
        docker_executable: str = "docker",
        ssh_executable: str = "ssh",
        scp_executable: str = "scp",
    ) -> None:
        self.docker_executable = docker_executable
        self.ssh_executable = ssh_executable
        self.scp_executable = scp_executable

    @staticmethod
    def _run(
        args: list[str],
        *,
        timeout: int,
        operation: str,
        input_text: str | None = None,
    ) -> str:
        options: dict[str, Any] = {
            "text": True,
            "capture_output": True,
            "timeout": timeout,
            "check": False,
            "start_new_session": True,
        }
        if input_text is None:
            options["stdin"] = subprocess.DEVNULL
        else:
            options["input"] = input_text
        try:
            result = subprocess.run(args, **options)
        except FileNotFoundError as exc:
            raise IntegrationNotConfiguredError(
                f"required executable is unavailable: {args[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RitsSshError(f"{operation} timed out after {timeout} seconds") from exc
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        if result.returncode != 0:
            raise RitsSshError(
                f"{operation} failed with exit code {result.returncode}: {output}"
            )
        return result.stdout.strip()

    def _context_endpoint(
        self, request: RitsSshFinetuneRequest, *, create: bool
    ) -> str:
        names = self._run(
            [self.docker_executable, "context", "ls", "--format", "{{.Name}}"],
            timeout=30,
            operation="list Docker contexts",
        ).splitlines()
        if request.docker_context not in names and create:
            self._run(
                [
                    self.docker_executable,
                    "context",
                    "create",
                    request.docker_context,
                    "--docker",
                    f"host={request.docker_endpoint}",
                ],
                timeout=60,
                operation=f"create Docker context {request.docker_context}",
            )
        elif request.docker_context not in names:
            raise IntegrationNotConfiguredError(
                f"Docker context {request.docker_context!r} is not registered; "
                "submit the task first or create the context explicitly"
            )
        endpoint = self._run(
            [
                self.docker_executable,
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
                request.docker_context,
            ],
            timeout=30,
            operation=f"inspect Docker context {request.docker_context}",
        )
        if endpoint != request.docker_endpoint:
            raise RitsSshError(
                f"Docker context {request.docker_context!r} already points to {endpoint!r}, "
                f"not {request.docker_endpoint!r}; choose another docker_context or fix it manually"
            )
        return endpoint

    def _docker(
        self,
        request: RitsSshFinetuneRequest,
        args: list[str],
        *,
        timeout: int,
        operation: str,
        create_context: bool = False,
    ) -> str:
        self._context_endpoint(request, create=create_context)
        return self._run(
            [self.docker_executable, "--context", request.docker_context, *args],
            timeout=timeout,
            operation=operation,
        )

    def _ssh_args(self, request: RitsSshFinetuneRequest) -> list[str]:
        args = [
            self.ssh_executable,
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={request.connect_timeout}",
        ]
        if request.port != 22:
            args.extend(("-p", str(request.port)))
        args.append(request.target)
        return args

    def _scp_args(self, request: RitsSshFinetuneRequest) -> list[str]:
        args = [
            self.scp_executable,
            "-r",
            "-p",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={request.connect_timeout}",
        ]
        if request.port != 22:
            args.extend(("-P", str(request.port)))
        return args

    def _ssh(self, request: RitsSshFinetuneRequest, command: str, *, operation: str) -> str:
        return self._run(
            [*self._ssh_args(request), command],
            timeout=request.connect_timeout + 60,
            operation=operation,
        )

    def submit(self, request: RitsSshFinetuneRequest, job_id: str) -> dict[str, Any]:
        if not request.bundle_dir:
            raise InvalidRequestError("bundle_dir is required for SSH fine-tuning")
        bundle = Path(request.bundle_dir).expanduser()
        try:
            bundle = bundle.resolve(strict=True)
        except OSError as exc:
            raise InvalidRequestError(f"training bundle does not exist: {request.bundle_dir}") from exc
        if not bundle.is_dir():
            raise InvalidRequestError(f"training bundle is not a directory: {bundle}")

        endpoint = self._context_endpoint(request, create=True)
        docker_version = self._docker(
            request,
            ["info", "--format", "{{.ServerVersion}}"],
            timeout=request.connect_timeout + 60,
            operation="query remote Docker daemon",
        )
        remote_dir = request.remote_job_dir(job_id)
        self._ssh(
            request,
            f"set -eu; mkdir -p -- {shlex.quote(request.remote_root)}; "
            f"mkdir -- {shlex.quote(remote_dir)}",
            operation="create remote task directory",
        )
        scp_args = self._scp_args(request)
        scp_args.extend((f"{bundle}/.", f"{request.target}:{remote_dir}/"))
        self._run(
            scp_args,
            timeout=request.transfer_timeout,
            operation="upload training bundle over scp",
        )

        gpu_selection = (
            "all"
            if request.container_gpus == "all"
            else f"device={request.container_gpus}"
        )
        container_name = request.container_name(job_id)
        container_command = shlex.join(request.command)
        container_id = self._docker(
            request,
            [
                "run",
                "--detach",
                "--pull=missing",
                "--name",
                container_name,
                "--label",
                "tricycle.managed-by=tricycle-agent",
                "--label",
                f"tricycle.job-id={job_id}",
                "--gpus",
                gpu_selection,
                "--mount",
                f"type=bind,source={remote_dir},target={request.container_workdir}",
                "--workdir",
                request.container_workdir,
                "--env",
                "WANDB_MODE=disabled",
                "--env",
                "WANDB_DISABLED=true",
                request.container_image or "",
                "/bin/bash",
                "-c",
                container_command,
            ],
            timeout=request.transfer_timeout,
            operation="start remote GPU training container",
        )
        return {
            "job_id": job_id,
            "state": "running",
            "container_id": container_id,
            "container_name": container_name,
            "container_runtime": "docker",
            "docker_context": request.docker_context,
            "docker_endpoint": endpoint,
            "docker_server_version": docker_version,
            "container_image": request.container_image,
            "container_gpus": request.container_gpus,
            "remote_dir": remote_dir,
            "target": request.target,
        }

    def status(self, request: RitsSshFinetuneRequest, job_id: str) -> dict[str, Any]:
        container_name = request.container_name(job_id)
        template = "{{.State.Status}}|{{.State.ExitCode}}|{{.State.StartedAt}}|{{.State.FinishedAt}}"
        try:
            output = self._docker(
                request,
                ["container", "inspect", "--format", template, container_name],
                timeout=request.connect_timeout + 60,
                operation="inspect remote training container",
            )
        except RitsSshError as exc:
            if "no such object" in str(exc).lower() or "no such container" in str(exc).lower():
                return {"job_id": job_id, "container_name": container_name, "state": "missing"}
            raise
        parts = output.split("|", maxsplit=3)
        if len(parts) != 4:
            raise RitsSshError(f"unexpected docker inspect status output: {output!r}")
        docker_state, exit_code, started_at, finished_at = parts
        if docker_state == "running":
            state = "running"
        elif docker_state == "exited":
            state = "succeeded" if exit_code == "0" else "failed"
        elif docker_state in {"created", "restarting", "paused"}:
            state = docker_state
        else:
            state = "failed" if docker_state == "dead" else docker_state
        return {
            "job_id": job_id,
            "container_name": container_name,
            "state": state,
            "docker_state": docker_state,
            "exit_code": exit_code,
            "started_at": started_at,
            "finished_at": finished_at,
            "remote_dir": request.remote_job_dir(job_id),
        }

    def logs(self, request: RitsSshFinetuneRequest, job_id: str) -> str:
        return self._docker(
            request,
            ["logs", "--tail", str(request.log_lines), request.container_name(job_id)],
            timeout=request.connect_timeout + 60,
            operation="read remote container logs",
        )

    def terminate(self, request: RitsSshFinetuneRequest, job_id: str) -> dict[str, Any]:
        output = self._docker(
            request,
            ["stop", "--time", "30", request.container_name(job_id)],
            timeout=request.connect_timeout + 90,
            operation="stop remote training container",
        )
        return {
            "job_id": job_id,
            "container_name": request.container_name(job_id),
            "state": "stopped",
            "raw": output,
        }

    def download(self, request: RitsSshFinetuneRequest, job_id: str) -> tuple[str, ...]:
        if not request.download_paths or not request.download_dir:
            raise InvalidRequestError("download_paths and download_dir are required")
        destination = Path(request.download_dir).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        remote_dir = request.remote_job_dir(job_id)
        sources = [f"{request.target}:{remote_dir}/{relative}" for relative in request.download_paths]
        args = [*self._scp_args(request), *sources, str(destination)]
        self._run(
            args,
            timeout=request.transfer_timeout,
            operation="download selected training outputs over scp",
        )
        return tuple(str(path) for path in sorted(destination.rglob("*")) if path.is_file())
