"""Prepare per-model Bohr environments. Default plan is offline and read-only."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

if __package__ in {None, ""}:
    # Standalone workers deliver shared modules beside sar_model_envs.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from sar_runtime.io import write_json  # type: ignore[import-not-found]  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from sar_runtime.toml import dump_toml as write_toml  # type: ignore[import-not-found]  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from sar_runtime.training_protocol import verify_asset_files as asset_check  # type: ignore[import-not-found]  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
except ImportError:
    from tricycle_core.io import write_json
    from tricycle_core.toml import dump_toml as write_toml
    from tricycle_core.training_protocol import verify_asset_files as asset_check

HERE = Path(__file__).resolve().parent
PYPI_INDEX_URL = "https://pypi.org/simple"
INITIALIZATION_BUDGET_SECONDS = 1800
INSTALL_MAX_ATTEMPTS = 3


class InitializationFailure(RuntimeError):
    reason: str
    next_action: str

    def __init__(self, reason: str, next_action: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.next_action = next_action


def retryable_install_failure(
    error: Exception, output: str, *, mirror_fallback: bool = False
) -> bool:
    """Retry transport failures only; an invalid dependency/asset stays invalid."""
    text = output.lower()
    if any(
        marker in text
        for marker in (
            "hash mismatch",
            "checksum mismatch",
            "do not match the hashes",
            "resolutionimpossible",
            "conflicting dependencies",
            "requires a different python",
            "certificate_verify_failed",
            "out of memory",
            "no space left on device",
        )
    ):
        return False
    return (
        isinstance(error, subprocess.TimeoutExpired)
        or (
            mirror_fallback
            and "from versions: none" in text
            and "no matching distribution found" in text
        )
        or any(
            marker in text
            for marker in (
                "readtimeouterror",
                "connecttimeouterror",
                "connectionerror",
                "proxyerror",
                "temporary failure in name resolution",
                "name or service not known",
                "connection broken",
                "connection reset",
                "connection aborted",
                "connection refused",
                "remote end closed",
                "network is unreachable",
                "could not resolve host",
                "failed to establish a new connection",
                "timed out",
                "early eof",
            )
        )
        or bool(
            re.search(
                r"(?:http(?:/\d(?:\.\d)?)?\s*(?:error)?[: /]+|(?:status(?: code)?|error|response):?\s+)(?:403|429|5\d\d)\b|\b(?:403|429|5\d\d)\s+(?:client|server)\s+error\b",
                text,
            )
        )
    )


def run_logged_command(
    argv: list[str],
    *,
    log_path: Path,
    env: dict[str, str],
    cwd: Path | None,
    capture: bool,
    timeout: float,
) -> str:
    """Stream one command and reap its process group before a timed-out retry."""
    with log_path.open("a") as log:
        print("[init] " + shlex.join(argv), flush=True)
        log.write("$ " + shlex.join(argv) + "\n")
        log.flush()
        offset = log.tell()
        stopped = threading.Event()

        def follow_log() -> None:
            with log_path.open() as reader:
                reader.seek(offset)
                while True:
                    chunk = reader.read()
                    if chunk:
                        print(chunk, end="", flush=True)
                    if stopped.is_set():
                        break
                    stopped.wait(0.2)

        follower = threading.Thread(target=follow_log, daemon=True)
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE if capture else log,
            stderr=log,
            start_new_session=True,
        )
        follower.start()

        def terminate(signum: int, frame: Any) -> None:
            raise SystemExit(128 + signum)

        previous = None
        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGTERM, terminate)
        try:
            stdout, _ = process.communicate(timeout=timeout)
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, argv, output=stdout)
            return stdout or ""
        except BaseException as error:
            # Include children such as pip build-isolation subprocesses, even if pip exited.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            log.flush()
            with log_path.open() as reader:
                reader.seek(offset)
                output = reader.read()
            if isinstance(error, subprocess.TimeoutExpired):
                error.stderr = output.encode()
            elif isinstance(error, subprocess.CalledProcessError):
                error.stderr = output
            raise
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)
            stopped.set()
            follower.join(timeout=2)
            if process.stdout is not None:
                process.stdout.close()


def run_install_command(
    argv: list[str],
    *,
    log_path: Path,
    env: dict[str, str],
    deadline: float,
    attempts: list[dict[str, Any]],
    cwd: Path | None = None,
    capture: bool = False,
) -> str:
    """Bound retries across initialization; keep successful work and the pip cache."""
    command, command_env = list(argv), dict(env)
    pip_install = command[1:4] == ["-m", "pip", "install"]
    transport = pip_install or command[0] == "curl" or command[:2] == ["git", "fetch"]
    began = time.monotonic()
    for attempt in range(1, INSTALL_MAX_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InitializationFailure(
                "initialization_budget_exhausted",
                "retry_job",
                "Initialization time budget exhausted",
            )
        event: dict[str, Any] = {
            "command": shlex.join(command),
            "attempt": attempt,
            "status": "interrupted",
            "retry": False,
        }
        started = time.monotonic()
        failure: Exception | None = None
        retry = False
        try:
            output = run_logged_command(
                command,
                log_path=log_path,
                env=command_env,
                cwd=cwd,
                capture=capture,
                timeout=min(450 if pip_install else 900, remaining),
            )
            event.update(status="succeeded", retry=False)
            return output
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            failure = error
            # Only this attempt's output participates in classification.
            diagnostic = error.stderr or ""
            tail = (
                diagnostic.decode(errors="replace") if isinstance(diagnostic, bytes) else diagnostic
            )[-16000:]
            mirror_denied = bool(re.search(r"\b403\b", tail))
            can_switch = (
                pip_install
                and not any(
                    arg in {"--index-url", "-i"} or arg.startswith("--index-url=")
                    for arg in command
                )
                and command_env.get("PIP_INDEX_URL") != PYPI_INDEX_URL
            )
            transient = transport and retryable_install_failure(
                error, tail, mirror_fallback=can_switch
            )
            if mirror_denied and not can_switch:
                transient = False
            if can_switch and transient:
                command += ["--index-url", PYPI_INDEX_URL]
                command_env = pypi_fallback_environment(command_env)
                transient = True
            delay = min(5 * 3 ** (attempt - 1), 30)
            retry = (
                transient and attempt < INSTALL_MAX_ATTEMPTS and time.monotonic() + delay < deadline
            )
            event.update(
                status="failed",
                retry=retry,
                reason="transient_network" if transient else "command_failed",
                delay_seconds=delay if retry else 0,
            )
        except OSError as error:
            failure = error
            event.update(status="failed", reason="command_failed", delay_seconds=0)
        finally:
            event.update(
                elapsed_seconds=round(time.monotonic() - started, 3),
                command_elapsed_seconds=round(time.monotonic() - began, 3),
                budget_remaining_seconds=round(max(0, deadline - time.monotonic()), 3),
            )
            attempts.append(event)
            print("[init-attempt] " + json.dumps(event), flush=True)
        if retry:
            time.sleep(event["delay_seconds"])
            continue
        raise InitializationFailure(
            "initialization_network_exhausted"
            if event["reason"] == "transient_network"
            else "initialization_command_failed",
            "retry_job" if event["reason"] == "transient_network" else "inspect_error",
            f"Initialization command failed after {attempt} attempt(s): {shlex.join(command)}",
        ) from failure
    raise AssertionError("Unreachable initialization retry state")


def cuda_preflight(env: dict[str, str], expected_torch: str) -> dict[str, Any]:
    """Check the inherited runtime and retain evidence without guessing its cause."""
    command = [
        sys.executable,
        "-c",
        """
import ctypes,json,os,subprocess,sys
from pathlib import Path
report = {"passed": False, "python": sys.executable,
          "environment": {key: os.environ.get(key, "") for key in
                          ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONPATH",
                           "PYTHONHOME", "CUDA_VISIBLE_DEVICES")}}
try:
    import torch
    report["torch"] = str(torch.__version__)
    report["torch_file"] = torch.__file__
    report["cuda_runtime"] = torch.version.cuda
    if report["torch"] != sys.argv[1]:
        raise ValueError("base Torch differs from configured runtime")
    torch.cuda.init()
    torch.ones(1, device="cuda").add_(1)
    torch.cuda.synchronize()
    report.update(passed=True, device=torch.cuda.get_device_name(0))
except Exception as error:
    report["error"] = str(error)
try:
    report["kernel_driver"] = Path("/proc/driver/nvidia/version").read_text().strip()
    report["loaded_cuda_libraries"] = sorted({line.split()[-1] for line in
        Path("/proc/self/maps").read_text().splitlines()
        if "libcuda." in line or "libcudart." in line})
except OSError as error:
    report["driver_files_error"] = str(error)
try:
    version = ctypes.c_int()
    driver = ctypes.CDLL("libcuda.so.1")
    report["driver_api_status"] = driver.cuDriverGetVersion(ctypes.byref(version))
    report["driver_api_version"] = version.value
except (OSError, AttributeError) as error:
    report["driver_api_error"] = str(error)
try:
    status = subprocess.run(["nvidia-smi", "--query-gpu=driver_version,name",
                             "--format=csv,noheader"],
                            capture_output=True, text=True, timeout=5)
    report["nvidia_smi"] = status.stdout.strip()
    if status.returncode:
        report["nvidia_smi_error"] = status.stderr.strip()
except (OSError, subprocess.SubprocessError) as error:
    report["nvidia_smi_error"] = str(error)
print(json.dumps(report))
""",
        expected_torch,
    ]
    try:
        result = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=30, check=True
        )
        report = json.loads(result.stdout.splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as error:
        raise InitializationFailure("cuda_preflight_failed", "inspect_error", str(error)) from error
    print("[init-preflight] " + json.dumps(report), flush=True)
    if not report["passed"]:
        raise InitializationFailure(
            "cuda_preflight_failed",
            "inspect_error",
            report.get("error", "CUDA preflight failed"),
        )
    return report


def pypi_fallback_environment(base: dict[str, str]) -> dict[str, str]:
    """Return a clean pip environment for the official-index retry.

    Bohr images can inject pip index settings outside pip.conf. An explicit
    ``--index-url`` normally takes precedence, but keeping those settings in
    the environment can still leave pip using the blocked mirror. Preserve
    unrelated environment variables and clear only pip source selectors.
    """
    result = dict(base)
    for key in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_NO_INDEX"):
        result.pop(key, None)
    result["PIP_CONFIG_FILE"] = "/dev/null"
    result["PIP_INDEX_URL"] = PYPI_INDEX_URL
    return result


def make_plan(model: str, root: Path, recipe_directory: Path | None = None) -> dict[str, Any]:
    recipe_directory = recipe_directory or HERE
    catalog = tomllib.loads((recipe_directory / "recipes.toml").read_text())
    spec = catalog["models"][model]
    identity = hashlib.sha256(
        Path(__file__).read_bytes()
        + b"".join(
            (recipe_directory / name).read_bytes()
            for name in ["recipes.toml", "verify.py"]
        )
    ).hexdigest()
    directory = root.resolve() / f"{model}-{identity[:12]}"
    return {
        "model": model,
        "recipe_sha256": identity,
        "directory": str(directory),
        "mode": spec["mode"],
        "repository": spec["repository"],
        "revision": spec["revision"],
        "python": spec.get("python", catalog["base_python"]),
        "torch": spec.get("expected_torch", catalog["base_torch"]),
        "requirements": spec["requirements"],
        "network_during_execute": True,
        "training_started": False,
        "download_assets": False,
        "asset_files": [e["path"] for e in spec.get("assets", [])],
    }


def validate_base_versions(actual: list[str], runtime: str, python_version: str) -> str:
    """Runtime CUDA identity and pip distribution identity are distinct."""
    if len(actual) != 3 or actual[:2] != [runtime, python_version]:
        raise RuntimeError(f"base environment differs from tested Bohr image: {actual}")
    return actual[2]


def initialize(args: argparse.Namespace) -> dict[str, Any]:
    if args.download_assets and args.model != "reactiont5":
        raise ValueError("--download-assets only supports ReactionT5")
    plan = make_plan(args.model, args.root)
    if not args.execute:
        return {**plan, "status": "plan", "download_assets": args.download_assets}
    if platform.system() != "Linux":
        raise RuntimeError("Execute on Linux/Bohr; use the default plan on other hosts")
    catalog = tomllib.loads((HERE / "recipes.toml").read_text())
    spec = catalog["models"][args.model]
    directory = Path(plan["directory"])
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".init.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return install_locked(args, plan, spec, directory)


def install_locked(
    args: argparse.Namespace, plan: dict[str, Any], spec: dict[str, Any], directory: Path
) -> dict[str, Any]:
    source, prefix, assets = directory / "source", directory / "env", directory / "assets"
    python = prefix / "bin/python"
    env = {
        **os.environ,
        "GIT_LFS_SKIP_SMUDGE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONNOUSERSITE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "PIP_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple",
        "PIP_CONFIG_FILE": "/dev/null",
        "PIP_DEFAULT_TIMEOUT": "30",
        "PIP_RETRIES": "2",
    }
    profile = tomllib.loads((HERE / "recipes.toml").read_text()).get("framework", {})
    if profile.get("official_index", False):
        env = pypi_fallback_environment(env)
    # Never inherit another model's import paths.
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    # Bohr base-image proxy can reject GitHub/Conda with CONNECT 503.
    for key in list(env):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            env.pop(key)
    env["NO_PROXY"] = env["no_proxy"] = "*"
    env.pop("PIP_NO_CACHE_DIR", None)
    env["PIP_CACHE_DIR"] = str(directory.parent / ".pip-cache")
    deadline = time.monotonic() + INITIALIZATION_BUDGET_SECONDS
    attempts: list[dict[str, Any]] = []

    def run(
        argv: list[str],
        *,
        cwd: Path | None = None,
        capture: bool = False,
        run_env: dict[str, str] | None = None,
    ) -> str:
        return run_install_command(
            argv,
            log_path=directory / "install.log",
            env=run_env or env,
            deadline=deadline,
            attempts=attempts,
            cwd=cwd,
            capture=capture,
        )

    state = {
        **plan,
        "status": "installing",
        "model_forward_tested": False,
        "benchmark_ready": False,
        "initialization_attempts": attempts,
        "initialization_budget_seconds": INITIALIZATION_BUDGET_SECONDS,
    }
    write_toml(directory / "state.toml", state)
    shutil.copy2(HERE / "recipes.toml", directory / "recipes.toml")
    try:
        if spec["mode"] == "base-venv" and spec.get("require_cuda", True):
            state["cuda_preflight"] = cuda_preflight(env, plan["torch"])
        if spec["mode"] == "system-venv":
            if not python.exists():
                run([sys.executable, "-m", "venv", "--system-site-packages", str(prefix)])
        elif spec["mode"] == "isolated-venv":
            actual_python = f"{sys.version_info.major}.{sys.version_info.minor}"
            if actual_python != plan["python"]:
                raise RuntimeError(
                    f"Python differs from pinned isolated environment: {actual_python}"
                )
            if not python.exists():
                run([sys.executable, "-m", "venv", str(prefix)])
        elif spec["mode"] == "base-venv":
            actual = run(
                [
                    sys.executable,
                    "-c",
                    'import torch,sys,importlib.metadata; print(str(torch.__version__)); print("%s.%s" % sys.version_info[:2]); print(importlib.metadata.version("torch"))',
                ],
                capture=True,
            ).splitlines()
            state["base_torch_distribution_version"] = validate_base_versions(
                actual, plan["torch"], plan["python"]
            )
            if not python.exists():
                run([sys.executable, "-m", "venv", "--system-site-packages", str(prefix)])
            if profile.get("inherit_runtime_sites", False):
                # A venv created from another venv does not inherit its parent site-packages.
                # Import the already verified runtime explicitly, without downloading a different Torch.
                sites = json.loads(
                    run(
                        [
                            sys.executable,
                            "-c",
                            "import json,site; print(json.dumps(site.getsitepackages()))",
                        ],
                        capture=True,
                    )
                )
                purelib = Path(
                    run(
                        [
                            str(python),
                            "-c",
                            'import sysconfig; print(sysconfig.get_path("purelib"))',
                        ],
                        capture=True,
                    ).strip()
                )
                (purelib / "tricycle_base_runtime.pth").write_text(
                    "".join(
                        str(Path(path).resolve()) + "\n" for path in sites if Path(path).is_dir()
                    )
                )
                inherited = run(
                    [
                        str(python),
                        "-c",
                        'import torch,sys,importlib.metadata; print(str(torch.__version__)); print("%s.%s" % sys.version_info[:2]); print(importlib.metadata.version("torch"))',
                    ],
                    capture=True,
                ).splitlines()
                if inherited != actual:
                    raise RuntimeError(
                        f"Model venv did not inherit the verified Torch runtime: {inherited}"
                    )
                state["inherited_runtime_site_packages"] = sites
        else:
            if args.conda_executable:
                if not args.conda_sha256:
                    raise ValueError("--conda-executable requires --conda-sha256")
                with args.conda_executable.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != args.conda_sha256:
                        raise ValueError("Conda executable checksum mismatch")
                manager = str(args.conda_executable.resolve())
                state["conda_executable_sha256"] = args.conda_sha256
            else:
                bootstrap = tomllib.loads((HERE / "recipes.toml").read_text())["bootstrap"]
                executable = directory / "micromamba"
                if not executable.exists():
                    archive_path = directory / "micromamba.tar.bz2"
                    run(
                        [
                            "curl",
                            "--fail",
                            "--location",
                            "--retry",
                            "2",
                            "--max-time",
                            "120",
                            "--output",
                            str(archive_path),
                            bootstrap["micromamba_url"],
                        ]
                    )
                    with archive_path.open("rb") as stream:
                        if (
                            hashlib.file_digest(stream, "sha256").hexdigest()
                            != bootstrap["micromamba_archive_sha256"]
                        ):
                            raise ValueError("micromamba archive checksum mismatch")
                    with tarfile.open(archive_path) as archive:
                        binary_member = archive.extractfile("bin/micromamba")
                        if binary_member is None:
                            raise ValueError("micromamba executable missing from archive")
                        executable.write_bytes(binary_member.read())
                    executable.chmod(0o755)
                with executable.open("rb") as stream:
                    if (
                        hashlib.file_digest(stream, "sha256").hexdigest()
                        != bootstrap["micromamba_binary_sha256"]
                    ):
                        raise ValueError("micromamba executable checksum mismatch")
                manager = str(executable)
                state["conda_executable_sha256"] = bootstrap["micromamba_binary_sha256"]
            env["MAMBA_ROOT_PREFIX"] = str(directory.parent / ".mamba-cache")
            state["conda_version"] = run([manager, "--version"], capture=True).strip()
            certificate = Path("/etc/ssl/certs/ca-certificates.crt")
            if not certificate.is_file():
                certificate = Path(ssl.get_default_verify_paths().cafile or "")
            if not certificate.is_file():
                raise RuntimeError("no existing CA bundle found for micromamba TLS verification")
            env["MAMBA_SSL_VERIFY"] = str(certificate)
            env["REQUESTS_CA_BUNDLE"] = str(certificate)
            channel = spec.get("conda_channel", "conda-forge")
            state["conda_channel"] = channel
            run(
                [
                    manager,
                    "--no-rc",
                    "install" if python.exists() else "create",
                    "-y",
                    "-p",
                    str(prefix),
                    "--override-channels",
                    "-c",
                    channel,
                    *spec["conda_packages"],
                ]
            )
        constraints = directory / "constraints.txt"
        constraints.write_text(
            "torch==" + state.get("base_torch_distribution_version", plan["torch"]) + "\n"
            if spec["mode"] != "system-venv"
            else ""
        )
        pip = [str(python), "-m", "pip", "install", "--constraint", str(constraints)]
        if profile.get("bootstrap_build_tools", False):
            run(
                [
                    *pip,
                    "--index-url",
                    PYPI_INDEX_URL,
                    "setuptools==80.9.0",
                    "wheel==0.43.0",
                    "setuptools-scm==10.2.1",
                ]
            )
        supplied_wheel = getattr(args, "torch_wheel", None)
        supplied_digest = getattr(args, "torch_wheel_sha256", None)
        if supplied_wheel is None and spec.get("torch_wheel_url"):
            url = spec["torch_wheel_url"]
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.netloc not in {
                "download.pytorch.org",
                "download-r2.pytorch.org",
            }:
                raise ValueError("Torch wheel must come from the official HTTPS distribution")
            supplied_wheel = directory / unquote(Path(parsed.path).name)
            supplied_digest = spec["torch_wheel_sha256"]
            if not supplied_wheel.exists():
                run(
                    [
                        "curl",
                        "--fail",
                        "--location",
                        "--retry",
                        "2",
                        "--max-time",
                        "900",
                        "--output",
                        str(supplied_wheel),
                        url,
                    ]
                )
        if supplied_wheel is not None:
            with supplied_wheel.open("rb") as stream:
                wheel_digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if not supplied_digest or wheel_digest != supplied_digest:
                raise ValueError("torch wheel SHA256 mismatch")
            state["torch_wheel_sha256"] = supplied_digest
            run([*pip, str(supplied_wheel.resolve())])
        elif spec.get("torch"):
            run([*pip, "--index-url", spec["torch_index"], spec["torch"]])
        if spec.get("runtime_only"):
            # Model source and real assets are validated by the molecular runner.
            source.mkdir(exist_ok=True)
        elif args.source_archive:
            if not args.source_sha256:
                raise ValueError("--source-archive requires --source-sha256")
            with args.source_archive.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != args.source_sha256:
                    raise ValueError("source archive checksum mismatch")
            source.mkdir(exist_ok=True)
            with tarfile.open(args.source_archive) as archive:
                for member in archive.getmembers():
                    target = (source / member.name).resolve()
                    if not target.is_relative_to(source.resolve()) or not (
                        member.isfile() or member.isdir()
                    ):
                        raise ValueError("unsafe source archive entry")
                archive.extractall(source, filter="data")
            if (source / ".tricycle-source-revision").read_text().strip() != spec["revision"]:
                raise ValueError("source archive revision mismatch")
            state["source_archive_sha256"] = args.source_sha256
        else:
            if not (source / ".git").exists():
                source.mkdir(exist_ok=True)
                run(["git", "init", str(source)])
                run(["git", "remote", "add", "origin", spec["repository"]], cwd=source)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, env=env, capture_output=True, text=True
            ).stdout.strip()
            if head and head != "HEAD" and head != spec["revision"]:
                raise RuntimeError("refusing to overwrite an unrelated source checkout")
            if head != spec["revision"]:
                if spec.get("sparse_paths"):
                    run(["git", "sparse-checkout", "init", "--no-cone"], cwd=source)
                    run(
                        ["git", "sparse-checkout", "set", "--no-cone", *spec["sparse_paths"]],
                        cwd=source,
                    )
                run(["git", "fetch", "--depth", "1", "origin", spec["revision"]], cwd=source)
                run(["git", "checkout", "--detach", spec["revision"]], cwd=source)
        if spec.get("patch_vocab"):
            file = source / "t5chem/run_trainer.py"
            old = "vocab_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vocab/'+args.tokenizer+'.txt')"
            new = "vocab_path = args.vocab or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vocab/'+args.tokenizer+'.txt')"
            content = file.read_text()
            if old not in content and new not in content:
                raise RuntimeError("T5Chem vocab patch context changed")
            content = content.replace(old, new)
            # Pinned transformers 4.40 uses the original TrainingArguments field name.
            if "eval_strategy=" not in content and "evaluation_strategy=" not in content:
                raise RuntimeError("T5Chem evaluation strategy patch context changed")
            content = content.replace("eval_strategy=", "evaluation_strategy=")
            file.write_text(content)
            state["t5chem_training_arguments_compatibility"] = (
                "transformers-4.40-evaluation_strategy"
            )
        extra = ["--find-links", spec["find_links"]] if spec.get("find_links") else []
        if spec["requirements"]:
            run([*pip, *extra, *spec["requirements"]])
        for wheel in spec.get("local_wheels", []):
            if args.assets_directory is None:
                raise ValueError("Local wheel assets are required")
            if asset_check(args.assets_directory, [wheel]):
                raise ValueError("Local wheel asset is missing")
            run([*pip, "--no-deps", str(args.assets_directory / wheel["path"])])
        if spec["package"]:
            run(
                [
                    *pip,
                    *(["--no-deps"] if spec.get("no_deps") else []),
                    *(["--no-build-isolation"] if profile.get("no_build_isolation") else []),
                    "-e",
                    str(source),
                ]
            )
        env["PATH"] = str(prefix / "bin") + os.pathsep + env["PATH"]
        env["PYTHONPATH"] = os.pathsep.join(str(source / p) for p in spec.get("source_paths", []))
        if args.model == "reamvp":
            libraries = list(prefix.glob("lib/python*/site-packages/nvidia/*/lib"))
            libraries += list(prefix.glob("lib/python*/site-packages/torch/lib"))
            env["LD_LIBRARY_PATH"] = (
                os.pathsep.join(map(str, libraries)) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
            )
        exports = {
            "PATH": env["PATH"],
            "PYTHONPATH": env["PYTHONPATH"],
            "PYTHONNOUSERSITE": "1",
            "SAR_MODEL_SOURCE": str(source),
            "SAR_MODEL_ASSETS": str(assets),
        }
        if "LD_LIBRARY_PATH" in env:
            exports["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH"]
        write_json(directory / "runtime.json", exports)
        (directory / "activate.sh").write_text(
            "\n".join(f"export {k}={shlex.quote(v)}" for k, v in exports.items()) + "\n"
        )
        (directory / "pip-freeze.txt").write_text(
            run([str(python), "-m", "pip", "freeze", "--all"], capture=True)
        )
        # Preserve upstream metadata conflicts (e.g. old T5Chem metadata) as a separate diagnostic.
        validation = subprocess.run(
            [str(python), "-m", "pip", "check"], env=env, text=True, capture_output=True
        )
        (directory / "pip-check.txt").write_text(validation.stdout + validation.stderr)
        state["pip_check_passed"] = validation.returncode == 0
        if spec.get("cli"):
            run([str(prefix / "bin" / spec["cli"][0]), *spec["cli"][1:]])
        verify_spec = {
            "modules": spec["modules"],
            "expected_torch": plan["torch"] if spec["mode"] != "system-venv" else "",
            "require_cuda": spec.get("require_cuda", True),
            "model_probe": spec.get("model_probe", ""),
        }
        response = run(
            [str(python), str(HERE / "verify.py"), json.dumps(verify_spec)],
            cwd=source,
            capture=True,
        )
        checks = json.loads(
            [
                line.split("=", 1)[1]
                for line in response.splitlines()
                if line.startswith("TRICYCLE_RESULT=")
            ][-1]
        )
        write_toml(directory / "environment-smoke.toml", checks)
        state["model_forward_tested"] = checks.get("model_forward_passed", False)
        state["model_backward_tested"] = checks.get("model_backward_passed", False)
        if not checks["passed"]:
            raise RuntimeError("model imports/device smoke failed; see environment-smoke.toml")
        assets.mkdir(exist_ok=True)
        if args.download_assets:
            if args.model != "reactiont5":
                raise ValueError(
                    "--download-assets only supports the pinned ReactionT5 public checkpoint"
                )
            run(
                [
                    str(python),
                    "-c",
                    "from huggingface_hub import snapshot_download; import sys; snapshot_download(repo_id=sys.argv[1], revision=sys.argv[2], local_dir=sys.argv[3], allow_patterns=sys.argv[4:])",
                    spec["checkpoint_repository"],
                    spec["checkpoint_revision"],
                    str(assets),
                    *[e["path"] for e in spec["assets"]],
                ]
            )
        if args.assets_directory:
            missing = asset_check(args.assets_directory, spec.get("assets", []))
            if missing:
                raise ValueError(f"incomplete supplied assets: {missing}")
            for entry in spec.get("assets", []):
                target = assets / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                if (args.assets_directory / entry["path"]).resolve() != target.resolve():
                    shutil.copy2(args.assets_directory / entry["path"], target)
        missing = asset_check(assets, spec.get("assets", []))
        state.update(
            status="environment-installed",
            missing_assets=missing,
            assets_verified=not missing,
            dependency_import_smoke_passed=True,
        )
        if not state["pip_check_passed"]:
            state["status"] = "dependency-conflicts"
        elif missing:
            state["status"] = "environment-installed-assets-missing"
        return state
    except Exception as error:
        state.update(status="failed", error=str(error))
        if isinstance(error, InitializationFailure):
            state.update(
                failure_reason=error.reason,
                next_action=error.next_action,
                reschedule_required=error.next_action == "reschedule",
            )
        else:
            state.update(
                failure_reason="initialization_invalid",
                next_action="inspect_error",
                reschedule_required=False,
            )
        print(
            "[init-failure] "
            + json.dumps(
                {
                    key: state[key]
                    for key in ("failure_reason", "next_action", "reschedule_required", "error")
                }
            ),
            flush=True,
        )
        raise
    finally:
        write_toml(directory / "state.toml", state)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        choices=list(tomllib.loads((HERE / "recipes.toml").read_text())["models"]),
        required=True,
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--assets-directory", type=Path)
    parser.add_argument("--source-archive", type=Path)
    parser.add_argument("--source-sha256")
    parser.add_argument("--conda-executable", type=Path)
    parser.add_argument("--conda-sha256")
    parser.add_argument("--torch-wheel", type=Path)
    parser.add_argument("--torch-wheel-sha256")
    parser.add_argument("--download-assets", action="store_true")
    args = parser.parse_args()
    result = initialize(args)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result["status"] == "dependency-conflicts":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
