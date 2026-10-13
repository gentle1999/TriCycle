from __future__ import annotations

import json
import os
import random
import re
import shlex
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from tricycle_core import BohrJobSpec, load_dotenv
from tricycle_core.errors import IntegrationNotConfiguredError, InvalidRequestError, TriCycleError
from tricycle_core.protocols import BohrGateway
from tricycle_core.validation import validate_fields

from .config import load_toml


class BohrGatewayError(TriCycleError):
    """Raised when the local Bohr CLI cannot complete an operation."""


class BohrWorkflowGateway(BohrGateway, Protocol):
    def create_job_group(self, name: str, project_id: str) -> str: ...

    def terminate(self, external_id: str) -> str: ...


_CONFIG_ENV_KEYS = frozenset(
    {
        "BOHR_BIN",
        "BOHR_RETRY_MAX",
        "BOHR_RETRY_DELAY",
        "BOHR_RETRY_BACKOFF",
        "BOHR_RETRY_MAX_DELAY",
        "BOHR_RETRY_JITTER",
        "BOHR_SUBMIT_RETRY_ON",
        "BOHR_DOWNLOAD_RETRY_ON",
        "BOHR_SUBMIT_DEDUP",
        "BOHR_SUBMIT_LOOKBACK",
        "BOHR_SUBMIT_REQUIRE_DEDUP",
        "BOHR_JOB_GROUP_LOOKBACK",
    }
)


@dataclass(frozen=True, slots=True)
class BohrCliConfig:
    executable: str = "bohr"
    retry_max: int = 5
    retry_delay: float = 5.0
    retry_backoff: float = 2.0
    retry_max_delay: float = 120.0
    retry_jitter: float = 1.0
    submit_retry_mode: str = "transient"
    download_retry_mode: str = "all"
    submit_dedup: bool = True
    submit_lookback: int = 50
    require_submit_name: bool = False
    job_group_lookback: int = 50

    def __post_init__(self) -> None:
        if self.retry_max < 1:
            raise InvalidRequestError("retry_max must be positive")
        if self.retry_delay < 0 or self.retry_backoff < 1 or self.retry_max_delay < 0:
            raise InvalidRequestError("retry timing values are invalid")
        if self.submit_retry_mode not in {"transient", "all", "none"}:
            raise InvalidRequestError("submit_retry_mode must be transient, all, or none")
        if self.download_retry_mode not in {"transient", "all", "none"}:
            raise InvalidRequestError("download_retry_mode must be transient, all, or none")

    @classmethod
    def from_env(
        cls,
        env_file: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> BohrCliConfig:
        """Read runtime adapter overrides from the process environment or ``.env``.

        This is intentionally separate from declarative TOML configuration.
        Credentials and CLI runtime context remain environment-provided.  Only
        adapter policy keys are loaded here; the gateway loads credentials when
        it builds the child-process environment for a live CLI call.
        """

        values = dict(os.environ if environ is None else environ)
        if environ is None or env_file is not None:
            load_dotenv(env_file, environ=values, keys=_CONFIG_ENV_KEYS)

        def integer(name: str, default: int) -> int:
            try:
                return int(values.get(name, default))
            except ValueError:
                return default

        def number(name: str, default: float) -> float:
            try:
                return float(values.get(name, default))
            except ValueError:
                return default

        def boolean(name: str, default: bool) -> bool:
            value = values.get(name)
            if value is None:
                return default
            return value.strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            executable=values.get("BOHR_BIN", "bohr"),
            retry_max=integer("BOHR_RETRY_MAX", 5),
            retry_delay=number("BOHR_RETRY_DELAY", 5),
            retry_backoff=number("BOHR_RETRY_BACKOFF", 2),
            retry_max_delay=number("BOHR_RETRY_MAX_DELAY", 120),
            retry_jitter=number("BOHR_RETRY_JITTER", 1),
            submit_retry_mode=values.get("BOHR_SUBMIT_RETRY_ON", "transient"),
            download_retry_mode=values.get("BOHR_DOWNLOAD_RETRY_ON", "all"),
            submit_dedup=boolean("BOHR_SUBMIT_DEDUP", True),
            submit_lookback=integer("BOHR_SUBMIT_LOOKBACK", 50),
            require_submit_name=boolean("BOHR_SUBMIT_REQUIRE_DEDUP", False),
            job_group_lookback=integer("BOHR_JOB_GROUP_LOOKBACK", 50),
        )

    @classmethod
    def from_toml(cls, path: Path) -> BohrCliConfig:
        """Build adapter policy from a TOML file; secrets stay in the environment."""

        if path.suffix.lower() != ".toml":
            raise InvalidRequestError("Bohr adapter configuration must use a .toml file")
        document = load_toml(path)
        validate_fields(document, {"bohr", "retry"}, context="Bohr adapter configuration")
        raw_bohr = document.get("bohr", {})
        raw_retry = document.get("retry", {})
        if not isinstance(raw_bohr, dict) or not isinstance(raw_retry, dict):
            raise InvalidRequestError("Bohr TOML configuration sections must be tables")

        validate_fields(raw_bohr, {"executable"}, context="bohr")
        validate_fields(
            raw_retry,
            {
                "max_attempts", "delay", "backoff", "max_delay", "jitter",
                "submit_mode", "download_mode", "submit_dedup", "submit_lookback",
                "require_submit_name", "job_group_lookback",
            },
            context="retry",
        )

        def integer(value: object, name: str, default: int) -> int:
            try:
                if not isinstance(value, (str, int, float)):
                    raise TypeError("expected a numeric value")
                return int(value)
            except (TypeError, ValueError) as exc:
                raise InvalidRequestError(f"{name} must be an integer") from exc

        def number(value: object, name: str, default: float) -> float:
            try:
                if not isinstance(value, (str, int, float)):
                    raise TypeError("expected a numeric value")
                return float(value)
            except (TypeError, ValueError) as exc:
                raise InvalidRequestError(f"{name} must be a number") from exc

        def boolean(value: object, name: str, default: bool) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"1", "true", "yes", "on"}:
                    return True
                if normalized in {"0", "false", "no", "off"}:
                    return False
            raise InvalidRequestError(f"{name} must be a boolean")

        return cls(
            executable=str(raw_bohr.get("executable", "bohr")),
            retry_max=integer(raw_retry.get("max_attempts", 5), "retry.max_attempts", 5),
            retry_delay=number(raw_retry.get("delay", 5), "retry.delay", 5),
            retry_backoff=number(raw_retry.get("backoff", 2), "retry.backoff", 2),
            retry_max_delay=number(raw_retry.get("max_delay", 120), "retry.max_delay", 120),
            retry_jitter=number(raw_retry.get("jitter", 1), "retry.jitter", 1),
            submit_retry_mode=str(raw_retry.get("submit_mode", "transient")),
            download_retry_mode=str(raw_retry.get("download_mode", "all")),
            submit_dedup=boolean(raw_retry.get("submit_dedup", True), "retry.submit_dedup", True),
            submit_lookback=integer(
                raw_retry.get("submit_lookback", 50), "retry.submit_lookback", 50
            ),
            require_submit_name=boolean(
                raw_retry.get("require_submit_name", False),
                "retry.require_submit_name",
                False,
            ),
            job_group_lookback=integer(
                raw_retry.get("job_group_lookback", 50), "retry.job_group_lookback", 50
            ),
        )


@dataclass(frozen=True, slots=True)
class _CommandResult:
    returncode: int
    output: str


class BohrCliGateway:
    """Adapter for the documented ``bohr`` CLI.

    The workflow and plugin layers never invoke subprocesses directly.  This
    class is the only production boundary that knows the CLI spelling, retry
    policy, and the structured output envelope used by current ``bohr``
    releases.
    """

    # The current CLI exposes the public BOHR_ID as ``bohrId``.  The adjacent
    # ``id`` field is the platform's internal job id and is not accepted by
    # describe/download/terminate. Only public IDs cross this adapter boundary.
    _PUBLIC_JOB_ID_KEYS = (
        "bohrJobId",
        "bohr_job_id",
        "bohrId",
        "bohr_id",
        "BOHR_ID",
    )
    _SUBMIT_GROUP_ID_KEYS = (
        "bohrJobGroupId",
        "bohr_job_group_id",
        "groupId",
        "group_id",
        "bohrGroupId",
        "bohr_group_id",
        "job_group_id",
    )

    _TRANSIENT = re.compile(
        r"timeout|timed out|deadline exceeded|temporar(?:y|ily) unavailable|"
        r"connection (?:reset|refused|aborted|closed)|broken pipe|unexpected EOF|"
        r"TLS handshake timeout|i/o timeout|no such host|network is unreachable|"
        r"socket|dial tcp|client\.timeout|server closed|too many requests|rate limit|"
        r"HTTP[ /]*(408|425|429|500|502|503|504)|status[ =:]*(408|425|429|500|502|503|504)|"
        r"丢包|超时|网络|连接",
        re.IGNORECASE,
    )
    _CLI_ERROR = re.compile(
        r"(?:^|\s)Error:|unknown command|unknown flag|unknown shorthand flag|"
        r"required flag|invalid argument|请求错误| is invalid",
        re.IGNORECASE,
    )

    def __init__(
        self,
        config: BohrCliConfig | None = None,
        *,
        environ: dict[str, str] | None = None,
        env_file: Path | None = None,
    ):
        child_environ = dict(os.environ if environ is None else environ)
        # An injected environment remains authoritative for tests and callers
        # that deliberately provide a complete process environment.  The
        # default production gateway discovers the project .env automatically;
        # env_file opts an injected environment into the same behavior.
        if environ is None or env_file is not None:
            load_dotenv(env_file, environ=child_environ)
        self.environ = child_environ
        self.config = config or BohrCliConfig.from_env(environ=self.environ)
    def _run_once(self, args: list[str]) -> _CommandResult:
        # Bohr API calls and signed artifact transfers use the direct network.
        # Scope this to the CLI child so model traffic keeps its proxy settings.
        child_environ = dict(self.environ)
        for key in tuple(child_environ):
            if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
                child_environ.pop(key)
        child_environ.update({"NO_PROXY": "*", "no_proxy": "*"})
        try:
            completed = subprocess.run(
                [self.config.executable, *args],
                env=child_environ,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise IntegrationNotConfiguredError(
                f"Bohr CLI not found: {self.config.executable}"
            ) from exc
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        return _CommandResult(completed.returncode, output)

    @classmethod
    def _structured_error_retryable(cls, output: str) -> bool | None:
        """Return the current CLI's explicit retry decision, when present."""

        try:
            raw = cls._json_value(output)
        except BohrGatewayError:
            return None
        if not isinstance(raw, dict) or raw.get("ok") is not False:
            return None
        error = raw.get("error")
        if isinstance(error, dict) and isinstance(error.get("retryable"), bool):
            return bool(error["retryable"])
        # A structured ``ok=false`` response without a retry hint is still a
        # CLI/API error.  Do not retry it as if it were a transient transport
        # failure.
        return False

    def _run(self, args: list[str], *, mode: str) -> str:
        result = _CommandResult(1, "")
        delay = self.config.retry_delay
        attempts = 0
        for attempt in range(1, self.config.retry_max + 1):
            attempts = attempt
            result = self._run_once(args)
            cli_error = bool(self._CLI_ERROR.search(result.output))
            structured_retryable = self._structured_error_retryable(result.output)
            if result.returncode == 0 and not cli_error and structured_retryable is None:
                return result.output
            transient = bool(self._TRANSIENT.search(result.output))
            if structured_retryable is False:
                break
            if cli_error and not transient and structured_retryable is not True:
                break
            if attempt >= self.config.retry_max:
                break
            should_retry = mode == "all" or (
                mode == "transient" and transient
            )
            should_retry = should_retry or structured_retryable is True
            if not should_retry or mode == "none":
                break
            jitter = random.uniform(0, self.config.retry_jitter) if self.config.retry_jitter else 0
            time.sleep(delay + jitter)
            delay = min(self.config.retry_max_delay, delay * self.config.retry_backoff)
        raise BohrGatewayError(
            f"Bohr command failed after {attempts} attempt(s): "
            f"{shlex.join([self.config.executable, *args])}\n{result.output.strip()}"
        )

    @staticmethod
    def _job_id(output: str) -> str | None:
        try:
            raw = BohrCliGateway._json_value(output)
        except BohrGatewayError:
            raw = None
        if raw is not None:
            for key in BohrCliGateway._PUBLIC_JOB_ID_KEYS:
                value = BohrCliGateway._find_value(raw, {key})
                identifier = BohrCliGateway._identifier(value)
                if identifier is not None:
                    return identifier
        for line in output.splitlines():
            if re.match(
                r"^\s*(?:Bohr\s*Job\s*Id|bohr[_\s]*id)\s*:",
                line,
                re.IGNORECASE,
            ):
                identifier = BohrCliGateway._identifier(line.split(":", 1)[1])
                if identifier is not None:
                    return identifier
        return None

    @staticmethod
    def _group_id(output: str) -> str | None:
        try:
            raw = BohrCliGateway._json_value(output)
        except BohrGatewayError:
            raw = None
        if raw is not None:
            for key in BohrCliGateway._SUBMIT_GROUP_ID_KEYS:
                value = BohrCliGateway._find_value(raw, {key})
                identifier = BohrCliGateway._identifier(value)
                if identifier is not None:
                    return identifier
        found: str | None = None
        for line in output.splitlines():
            if re.match(
                r"^\s*(?:Bohr\s*Job\s*Group\s*Id|bohr[_\s]*group[_\s]*id|"
                r"group[_\s]*id)\s*:",
                line,
                re.IGNORECASE,
            ):
                identifier = BohrCliGateway._identifier(line.split(":", 1)[1])
                if identifier is not None:
                    found = identifier
        return found

    @staticmethod
    def _identifier(value: object | None) -> str | None:
        if value is None or isinstance(value, bool):
            return None
        text = str(value).strip()
        if not text or (text.isdigit() and int(text) <= 0):
            return None
        if text.isdigit():
            return text
        digits = re.sub(r"[^0-9]", "", text)
        return digits or None

    @staticmethod
    def _json_value(output: str) -> Any:
        decoder = json.JSONDecoder()
        for index, character in enumerate(output):
            if character not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(output[index:])
            except json.JSONDecodeError:
                continue
            return value
        raise BohrGatewayError("Bohr CLI did not return JSON")

    @staticmethod
    def _find_value(value: object, keys: set[str]) -> object | None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys and item is not None:
                    return item
            for item in value.values():
                found = BohrCliGateway._find_value(item, keys)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = BohrCliGateway._find_value(item, keys)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _result_file_selection(job: BohrJobSpec, parameters: Mapping[str, Any]) -> tuple[str, ...]:
        """Resolve TriCycle result declarations to Bohr ``backward_files``.

        Bohr CLI 2.x accepts the result allow-list in its JSON job config as
        ``backward_files`` (the API field is ``outFiles``), but does not expose
        it as a submit flag.  ``output_artifacts`` is the typed TriCycle
        declaration; batch requests use the platform field explicitly.
        """

        declared = parameters.get("backward_files")
        if declared is None:
            values: object = [
                (
                    f"{artifact.location.rstrip('/')}/"
                    if artifact.kind == "directory"
                    else artifact.location
                )
                for artifact in job.output_artifacts
            ]
        else:
            values = declared

        if isinstance(values, str):
            candidates = values.split(",")
        elif isinstance(values, (list, tuple)):
            candidates = [str(item) for item in values]
        elif values is None:
            candidates = []
        else:
            raise InvalidRequestError("backward_files must be a string or list")

        selectors: list[str] = []
        for item in candidates:
            selector = item.strip().replace("\\", "/")
            while selector.startswith("./"):
                selector = selector[2:]
            if not selector:
                continue
            if selector.startswith("/") or ".." in selector.split("/"):
                raise InvalidRequestError(
                    "Bohr backward_files selectors must be non-empty relative paths"
                )
            selectors.append(selector)
        return tuple(dict.fromkeys(selectors))

    @staticmethod
    def _result_selector_config(selectors: tuple[str, ...]) -> tempfile.TemporaryDirectory:
        """Create a short-lived config file for the CLI's result allow-list."""

        directory = tempfile.TemporaryDirectory(prefix="tricycle-bohr-submit-")
        config_path = Path(directory.name) / "job.json"
        config_path.write_text(
            json.dumps({"backward_files": list(selectors)}, ensure_ascii=False),
            encoding="utf-8",
        )
        return directory

    @staticmethod
    def _json_items(value: object) -> list[object]:
        """Return records from either a bare list or the current envelope."""

        if isinstance(value, dict) and "data" in value:
            value = value["data"]
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            value = value["items"]
        if isinstance(value, list):
            return value
        return [value]

    def list_group_jobs(self, job_group_id: str) -> tuple[dict[str, Any], ...]:
        """List all jobs in a group using the current CLI's group describe form."""

        group_id = str(job_group_id).strip()
        if not group_id:
            raise InvalidRequestError("job_group_id must not be empty")
        value = self._json_value(
            self._run(
                [
                    "job",
                    "describe",
                    "--job_group_id",
                    group_id,
                    "--output",
                    "json",
                ],
                mode="transient",
            )
        )
        items = self._json_items(value)
        records = tuple(item for item in items if isinstance(item, dict))
        if len(records) != len(items):
            raise BohrGatewayError("Bohr group job listing returned a non-object record")
        return records

    def submit(self, job: BohrJobSpec) -> str:
        if self.config.require_submit_name and not job.name.strip():
            raise InvalidRequestError("Bohr submit requires a stable job name")
        if not job.project_id or not job.machine_type:
            raise InvalidRequestError("Bohr job requires project_id and machine_type")
        parameters = dict(job.parameters)
        args = [
            "job",
            "submit",
            "--job_name",
            job.name,
            "--project-id",
            str(job.project_id),
            "--machine_type",
            job.machine_type,
            "--image_address",
            job.image,
        ]
        input_directory = parameters.get("input_directory")
        if input_directory:
            args.extend(["--input_directory", str(input_directory)])
        command_string = parameters.get("command_string")
        if command_string is None and job.command:
            command_string = shlex.join(job.command)
        if command_string:
            args.extend(["--command", str(command_string)])
        for key, flag in (
            ("log_file", "--log_file"),
            ("job_group_id", "--job_group_id"),
            ("result_path", "--result_path"),
            ("max_reschedule_times", "--max_reschedule_times"),
            ("max_run_time", "--max_run_time"),
            ("nnode", "--nnode"),
        ):
            value = parameters.get(key)
            if value not in (None, ""):
                args.extend([flag, str(value)])

        dataset_paths = tuple(
            str(item) for item in parameters.get("dataset_paths", ()) if str(item)
        )
        if dataset_paths:
            raise InvalidRequestError(
                "bohr 2.x has no --dataset_path submit flag; materialize datasets in "
                "input_directory or provide a dedicated adapter"
            )
        selectors = self._result_file_selection(job, parameters)
        selector_config: tempfile.TemporaryDirectory | None = None
        if selectors:
            selector_config = self._result_selector_config(selectors)
            # ``backward_files`` is a field in the CLI JSON config, not a
            # command-line flag.  Keep all ordinary values as explicit flags
            # so the generated file contains only the unsupported allow-list.
            args[2:2] = ["--config_file", str(Path(selector_config.name) / "job.json")]
        args.extend(str(item) for item in parameters.get("extra_submit_args", ()))
        # ``--output json`` is an output format, not a configuration file.  It
        # gives the adapter stable access to the public ``bohrJobId`` field.
        args.extend(["--output", "json"])

        submitted_after = datetime.now().astimezone()
        try:
            output = self._run(args, mode=self.config.submit_retry_mode)
        except BohrGatewayError as exc:
            if not self.config.submit_dedup or not job.name or not self._TRANSIENT.search(str(exc)):
                raise
            recovered = self._find_recent_job(
                job.name,
                str(parameters.get("job_group_id", "")),
                created_after=submitted_after,
            )
            if recovered is None:
                raise
            return recovered
        finally:
            if selector_config is not None:
                selector_config.cleanup()
        external_id = self._job_id(output)
        if external_id is not None:
            return external_id
        raise BohrGatewayError("Bohr submit succeeded without a JobId in its output")

    def _find_recent_job(
        self,
        name: str,
        group_id: str = "",
        *,
        created_after: datetime | None = None,
    ) -> str | None:
        args = [
            "job",
            "list",
            "--output",
            "json",
            "--number",
            str(self.config.submit_lookback),
        ]
        if group_id:
            args.extend(["--job_group_id", group_id])
        try:
            value = self._json_value(self._run(args, mode="transient"))
        except BohrGatewayError:
            return None
        items = self._json_items(value)
        for item in items:
            found_name = self._find_value(item, {"job_name", "jobName", "JobName", "name", "Name"})
            if str(found_name or "") != name:
                continue
            if created_after is not None:
                raw_created = self._find_value(
                    item,
                    {"create_time", "createTime", "created_at", "createdAt", "submit_time", "submitTime"},
                )
                created = self._parse_job_creation_time(raw_created, created_after.tzinfo)
                if created is None or created < created_after - timedelta(seconds=10):
                    continue
            found_id = self._job_id(json.dumps(item))
            if found_id is not None:
                return found_id
        return None

    @staticmethod
    def _parse_job_creation_time(value: Any, local_timezone: Any) -> datetime | None:
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value, UTC)
            except (OverflowError, OSError, ValueError):
                return None
        if not isinstance(value, str) or not value.strip():
            return None
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=local_timezone)
        return parsed

    def create_job_group(self, name: str, project_id: str) -> str:
        output = self._run(
            [
                "job_group",
                "create",
                "--name",
                name,
                "--project-id",
                str(project_id),
                "--output",
                "json",
            ],
            mode="transient",
        )
        group_id = self._group_id(output)
        if group_id is not None:
            return group_id
        try:
            value = self._json_value(
                self._run(
                    [
                        "job_group",
                        "list",
                        "--output",
                        "json",
                        "--number",
                        str(self.config.job_group_lookback),
                        "--project-id",
                        str(project_id),
                    ],
                    mode="transient",
                )
            )
        except BohrGatewayError as exc:
            raise BohrGatewayError("Bohr group creation returned no group id") from exc
        items = self._json_items(value)
        for item in items:
            found_name = self._find_value(
                item, {"job_group_name", "jobGroupName", "JobGroupName", "jobName", "name", "Name"}
            )
            if str(found_name or "") != name:
                continue
            found_id = self._group_id(json.dumps(item))
            if found_id is not None:
                return found_id
        raise BohrGatewayError("Bohr group creation returned no group id")

    def status(self, external_id: str) -> dict[str, Any]:
        output = self._run(
            ["job", "describe", "--id", str(external_id), "--output", "json"], mode="transient"
        )
        try:
            raw = self._json_value(output)
        except BohrGatewayError:
            return {"external_id": str(external_id), "status": output.strip(), "raw": output}
        # The current CLI returns both a numeric platform status (often
        # ``status=-1``) and the normalized lifecycle value in ``phase``.
        # Prefer the textual field so failed/finished jobs are not reported as
        # UNKNOWN merely because the numeric diagnostic field appears first.
        status = None
        for key in (
            "phase",
            "statusStr",
            "StatusStr",
            "status_string",
            "statusString",
            "state",
            "State",
            "statusName",
            "status_name",
            "job_status",
            "jobStatus",
            "JobStatus",
        ):
            candidate = self._find_value(raw, {key})
            if isinstance(candidate, str) and candidate.strip():
                status = candidate
                break
        if status is None:
            status = str(self._find_value(raw, {"status", "Status"}) or "")
        return {"external_id": str(external_id), "status": str(status or ""), "raw": raw}

    def download(self, external_id: str, destination: Path) -> tuple[str, ...]:
        destination.mkdir(parents=True, exist_ok=True)
        self._run(
            ["job", "download", str(external_id), "--out", str(destination)],
            mode=self.config.download_retry_mode,
        )
        return tuple(str(path) for path in sorted(destination.rglob("*")) if path.is_file())

    def log(self, external_id: str, destination: Path | None = None) -> str:
        args = ["job", "log", str(external_id)]
        if destination is not None:
            destination.mkdir(parents=True, exist_ok=True)
            args.extend(["--out", str(destination)])
        return self._run(args, mode=self.config.download_retry_mode)

    def terminate(self, external_id: str, *, confirm: bool = False) -> str:
        args = ["job", "terminate", str(external_id)]
        if confirm:
            args.append("--yes")
        return self._run(args, mode="transient")

    def terminate_group(self, group_id: str, *, confirm: bool = False) -> str:
        args = ["job_group", "terminate", str(group_id)]
        if confirm:
            args.append("--yes")
        return self._run(args, mode="transient")

@dataclass
class InMemoryBohrGateway:
    """Deterministic adapter for tests and local orchestration dry-runs."""

    jobs: dict[str, BohrJobSpec] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)
    groups: dict[str, tuple[str, str]] = field(default_factory=dict)
    terminated: list[str] = field(default_factory=list)
    _job_counter: int = 0
    _group_counter: int = 0

    def submit(self, job: BohrJobSpec) -> str:
        self._job_counter += 1
        external_id = f"memory/job-{self._job_counter}"
        self.jobs[external_id] = job
        self.statuses[external_id] = "PENDING"
        return external_id

    def status(self, external_id: str) -> dict[str, Any]:
        if external_id not in self.jobs:
            return {"external_id": external_id, "status": "UNKNOWN"}
        return {"external_id": external_id, "status": self.statuses.get(external_id, "PENDING")}

    def download(self, external_id: str, destination: Path) -> tuple[str, ...]:
        if external_id not in self.jobs:
            return ()
        destination.mkdir(parents=True, exist_ok=True)
        marker = destination / "memory-job.json"
        marker.write_text(json.dumps(self.jobs[external_id].to_dict(), indent=2), encoding="utf-8")
        return (str(marker),)

    def create_job_group(self, name: str, project_id: str) -> str:
        self._group_counter += 1
        group_id = f"memory/group-{self._group_counter}"
        self.groups[group_id] = (name, project_id)
        return group_id

    def terminate(self, external_id: str) -> str:
        if external_id in self.jobs:
            self.statuses[external_id] = "CANCELED"
        self.terminated.append(external_id)
        return f"terminated {external_id}"

    def terminate_group(self, group_id: str) -> str:
        self.terminated.append(group_id)
        return f"terminated {group_id}"
