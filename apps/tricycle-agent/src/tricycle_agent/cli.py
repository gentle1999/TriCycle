from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tricycle_core import (
    CapabilityCatalog,
    ExecutionContext,
    InvalidRequestError,
    RunStore,
    SideEffectLevel,
    TaskResult,
)

from .bootstrap import create_capability_catalog, create_registry
from .execution import CapabilityDispatcher
from .run_state import (
    _PREPARATION_CAPABILITIES,
    copy_request,
    open_or_create,
    record_result,
    requested_run_dir,
    resume_request,
)
from .toml_requests import load_request


def _read_toml_request(path: Path) -> dict[str, object]:
    """Compatibility wrapper for the legacy CLI's user-facing errors."""

    try:
        return load_request(path)
    except InvalidRequestError as exc:
        raise SystemExit(str(exc)) from exc


def _json(value: object, *, indent: int | None = None) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=True))


def _add_execution_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--workdir", type=Path, default=Path("."))
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--live", action="store_true", help="allow external side effects")
    parser.add_argument("--confirm", action="store_true", help="confirm destructive actions")
    parser.add_argument("--bohr-config", type=Path, default=None, help="Bohr adapter TOML")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tricycle-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("plugins", help="list registered implementation components")
    subparsers.add_parser("capabilities", help="list Codex-facing capability manifests")

    plan = subparsers.add_parser(
        "plan",
        help="build a dry-run plan; use '<capability> <request.toml>' for the new API",
    )
    plan.add_argument("target", help="capability ID or legacy plugin ID")
    plan.add_argument("operation_or_request")
    plan.add_argument("request", nargs="?", type=Path, help="legacy TOML request path")
    plan.add_argument("--run-id", default="cli-run")
    plan.add_argument("--workdir", type=Path, default=Path("."))

    prepare = subparsers.add_parser("prepare", help="legacy local plugin preparation")
    prepare.add_argument("plugin_id")
    prepare.add_argument("operation")
    prepare.add_argument("request", type=Path, help="path to a TOML request file")
    prepare.add_argument("--run-id", default="cli-run")
    prepare.add_argument("--workdir", type=Path, default=Path("."))

    run = subparsers.add_parser(
        "run", help="execute one independently callable capability and persist run.toml"
    )
    run.add_argument("capability_id")
    run.add_argument("request", type=Path, help="path to a TOML request file")
    _add_execution_options(run)

    inspect = subparsers.add_parser("inspect", help="inspect a persisted capability run")
    inspect.add_argument("--run-dir", type=Path, required=True)

    resume = subparsers.add_parser("resume", help="execute the next action recorded in a run.toml")
    resume.add_argument("--run-dir", type=Path, required=True)
    resume.add_argument("--workdir", type=Path, default=Path("."))
    resume.add_argument("--live", action="store_true", help="allow external side effects")
    resume.add_argument("--confirm", action="store_true", help="confirm destructive actions")
    resume.add_argument("--bohr-config", type=Path, default=None, help="Bohr adapter TOML")
    return parser


def _run_id_for(request_path: Path, requested: str | None) -> str:
    value = (requested or request_path.stem).strip()
    if not value:
        raise InvalidRequestError("run_id must not be empty")
    return value


def _run_directory_from_result(result: TaskResult, fallback: Path) -> Path:
    for artifact in result.outputs:
        if artifact.kind == "directory":
            return Path(artifact.location).resolve()
    return fallback.resolve()


def _failure_state(
    store: RunStore | None,
    *,
    capability_id: str,
    catalog: CapabilityCatalog,
    dry_run: bool,
    error: Exception,
) -> None:
    if store is None:
        return
    manifest = catalog.get(capability_id).manifest
    retry_action = ""
    if manifest.resumable:
        retry_action = capability_id
    current = store.read()
    execution = current.get("execution", {})
    try:
        attempt = int(execution.get("attempt", 0)) + 1 if isinstance(execution, dict) else 1
    except (TypeError, ValueError):
        attempt = 1
    store.update(
        status="failed",
        phase="error",
        next_action=retry_action,
        action=capability_id,
        attempt=attempt,
        execution={"dry_run": dry_run},
        error={"type": type(error).__name__, "message": str(error)},
    )


def _execute_capability(
    capability_id: str,
    request_path: Path,
    request: Mapping[str, Any],
    *,
    catalog: CapabilityCatalog,
    run_id: str,
    workdir: Path,
    run_dir: Path | None,
    live: bool,
    confirm: bool,
    bohr_config: Path | None,
    reuse_existing: bool,
) -> tuple[TaskResult, RunStore]:
    dry_run = not live
    effective_request = dict(request)
    if run_dir is not None:
        key = "out_dir" if capability_id in _PREPARATION_CAPABILITIES else "run_dir"
        effective_request[key] = str(run_dir)
    target_dir = requested_run_dir(effective_request, workdir, run_id)

    manifest = catalog.get(capability_id).manifest
    if live and manifest.side_effect is SideEffectLevel.DESTRUCTIVE and not confirm:
        raise InvalidRequestError(f"{capability_id} requires --confirm before a live execution")

    store: RunStore | None = None
    try:
        if capability_id not in _PREPARATION_CAPABILITIES:
            if not reuse_existing and (target_dir / "run.toml").is_file():
                raise InvalidRequestError(
                    f"run state already exists; use resume for continuation: {target_dir / 'run.toml'}"
                )
            store = open_or_create(
                target_dir,
                run_id=run_id,
                capability_id=capability_id,
                dry_run=dry_run,
            )
            copy_request(request_path, store.run_dir)
            store.update(execution={"dry_run": dry_run})

        dispatcher = CapabilityDispatcher(catalog)
        bohr_capability = capability_id.startswith(("bohr.", "sar.", "rits."))
        # Local preparation and pure planning may use Bohr-shaped request
        # models, but they do not need credentials or a live adapter.
        if live and bohr_capability and manifest.requires_auth:
            dispatcher = dispatcher.with_bohr_cli(bohr_config)

        if capability_id in _PREPARATION_CAPABILITIES:
            # Staging requires an empty output directory, so run.toml is created
            # immediately after the local materialization succeeds.
            result = dispatcher.execute(
                capability_id,
                effective_request,
                ExecutionContext(run_id, workdir, dry_run),
                confirm=confirm,
            )
            target_dir = _run_directory_from_result(result, target_dir)
            store = open_or_create(
                target_dir,
                run_id=run_id,
                capability_id=capability_id,
                dry_run=dry_run,
            )
        else:
            result = dispatcher.execute(
                capability_id,
                effective_request,
                ExecutionContext(run_id, workdir, dry_run),
                confirm=confirm,
            )

        copy_request(request_path, store.run_dir)
        record_result(store, capability_id, result)
        return result, store
    except Exception as exc:
        _failure_state(
            store,
            capability_id=capability_id,
            catalog=catalog,
            dry_run=dry_run,
            error=exc,
        )
        raise


def _run_command(args: argparse.Namespace, catalog: CapabilityCatalog) -> int:
    request = _read_toml_request(args.request)
    run_id = _run_id_for(args.request, args.run_id)
    result, store = _execute_capability(
        args.capability_id,
        args.request,
        request,
        catalog=catalog,
        run_id=run_id,
        workdir=args.workdir,
        run_dir=args.run_dir,
        live=args.live,
        confirm=args.confirm,
        bohr_config=args.bohr_config,
        reuse_existing=False,
    )
    payload = result.to_dict()
    payload["run_dir"] = str(store.run_dir)
    payload["state_file"] = str(store.path)
    _json(payload, indent=2)
    return 0


def _resume_command(args: argparse.Namespace, catalog: CapabilityCatalog) -> int:
    store = RunStore.open(args.run_dir)
    capability_id, request = resume_request(store)
    request_path = store.run_dir / "request.toml"
    run_data = store.read()
    run_section = run_data.get("run", {})
    if not isinstance(run_section, dict):
        raise InvalidRequestError(f"run.toml [run] must be a table: {store.path}")
    result, updated = _execute_capability(
        capability_id,
        request_path,
        request,
        catalog=catalog,
        run_id=str(run_section["id"]),
        workdir=args.workdir,
        run_dir=store.run_dir,
        live=args.live,
        confirm=args.confirm,
        bohr_config=args.bohr_config,
        reuse_existing=True,
    )
    payload = result.to_dict()
    payload["run_dir"] = str(updated.run_dir)
    payload["state_file"] = str(updated.path)
    _json(payload, indent=2)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "plugins":
        for manifest in create_registry().manifests():
            _json(manifest.to_dict())
        return 0

    if args.command == "capabilities":
        for binding in create_capability_catalog().bindings():
            _json(binding.to_dict())
        return 0

    if args.command == "plan":
        registry = create_registry()
        context = ExecutionContext(
            run_id=args.run_id,
            workdir=args.workdir,
            dry_run=True,
        )
        if args.request is None:
            request_path = Path(args.operation_or_request)
            request = _read_toml_request(request_path)
            plan = create_capability_catalog(registry).plan(args.target, request, context)
        else:
            request = _read_toml_request(args.request)
            plugin = registry.get(args.target)
            plan = plugin.plan(args.operation_or_request, request, context)
        _json(plan.to_dict(), indent=2)
        return 0

    if args.command == "prepare":
        request = _read_toml_request(args.request)
        plugin = create_registry().get(args.plugin_id)
        prepare = getattr(plugin, "prepare", None)
        if prepare is None:
            raise SystemExit(f"plugin does not support local preparation: {args.plugin_id}")
        context = ExecutionContext(run_id=args.run_id, workdir=args.workdir, dry_run=True)
        result = prepare(args.operation, request, context)
        _json(result.to_dict(), indent=2)
        return 0

    if args.command == "run":
        return _run_command(args, create_capability_catalog())

    if args.command == "inspect":
        _json(RunStore.open(args.run_dir).read(), indent=2)
        return 0

    if args.command == "resume":
        return _resume_command(args, create_capability_catalog())

    return 2
