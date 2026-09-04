from __future__ import annotations

import glob
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tricycle_core.errors import InvalidRequestError

from .models import DpaBernyRequest
from .utils import posix_cksum, sanitize_name, short_hash, worker_label


@dataclass(frozen=True, slots=True)
class DpaTask:
    source_xyz: Path
    worker: int
    task_name: str
    gjf_name: str
    json_names: tuple[str, ...]
    traj_names: tuple[str, ...]
    task_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_xyz": str(self.source_xyz),
            "worker": worker_label(self.worker, 1),
            "task_name": self.task_name,
            "gjf": self.gjf_name,
            "json": list(self.json_names),
            "traj": list(self.traj_names),
            "task_type": self.task_type,
        }


def expand_xyz_patterns(patterns: tuple[str, ...]) -> tuple[Path, ...]:
    """Expand files/directories/globs deterministically, like the old CLI."""

    matches: set[Path] = set()
    for pattern in patterns:
        path = Path(pattern)
        if path.is_file():
            matches.add(path.resolve())
            continue
        if path.is_dir():
            matches.update(item.resolve() for item in path.rglob("*.xyz") if item.is_file())
            continue
        # ``glob`` also accepts absolute patterns, which mirrors the legacy
        # shell entrypoint and is useful for data mounted outside the repo.
        for item_name in glob.glob(pattern, recursive=True):
            item = Path(item_name)
            if item.is_file() and item.suffix.lower() == ".xyz":
                matches.add(item.resolve())
    return tuple(sorted(matches, key=str))


def constraint_pairs(path: Path) -> tuple[tuple[int, int], ...]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        return ()
    marker = re.sub(r"\s+", "", lines[1])
    if not re.fullmatch(r"\d+,\d+(;\d+,\d+)*", marker):
        return ()
    return tuple(
        (int(left), int(right))
        for left, right in (item.split(",", 1) for item in marker.split(";"))
    )


def read_xyz_atoms(path: Path) -> tuple[tuple[str, float, float, float], ...]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise InvalidRequestError(f"xyz file is empty: {path}")
    try:
        atom_count = int(lines[0].strip().split()[0])
    except (IndexError, ValueError) as exc:
        raise InvalidRequestError(f"invalid xyz atom count: {path}") from exc
    if atom_count < 1 or len(lines) < atom_count + 2:
        raise InvalidRequestError(f"incomplete xyz file: {path}")
    atoms: list[tuple[str, float, float, float]] = []
    for line_number, line in enumerate(lines[2 : atom_count + 2], start=3):
        fields = line.split()
        if len(fields) < 4:
            raise InvalidRequestError(f"malformed xyz atom line {line_number}: {path}")
        try:
            atoms.append((fields[0], float(fields[1]), float(fields[2]), float(fields[3])))
        except ValueError as exc:
            raise InvalidRequestError(f"invalid xyz coordinates on line {line_number}: {path}") from exc
    return tuple(atoms)


def default_route(engine: str, task_type: str) -> str:
    if engine == "dpa":
        return {
            "ts": "opt(ts,nomicro,calcfc,noeigen,nofreeze) nosymm",
            "opt": "opt(nomicro) nosymm",
        }[task_type]
    if engine == "gaussian":
        return {
            "ts": "opt=(ts,readfc,noeigen,nofreeze,notrust,maxcycle=250) freq",
            "opt": "opt(nomicro)",
        }[task_type]
    raise InvalidRequestError(f"unsupported engine: {engine}")


def infer_task_type(path: Path) -> str:
    name = path.name
    if re.search(r"_ts(?:\.xyz)?$", name):
        return "ts"
    if re.search(r"_(?:pre|post)(?:\.xyz)?$", name):
        return "opt"
    raise InvalidRequestError(f"cannot infer task type from file name: {path}")


def gaussian_level_suffix(request: DpaBernyRequest) -> str:
    if request.functional is None and request.basis is None:
        return ""
    if not request.functional or not request.basis:
        raise InvalidRequestError("functional and basis must be set together")
    return f" {request.functional}/{request.basis}"


def build_route_line(
    engine: str,
    route: str,
    json_name: str,
    *,
    geom_suffix: str = "",
    level_suffix: str = "",
) -> str:
    if engine == "dpa":
        return f"#p {route}{level_suffix} external('gauext ./{json_name}'){geom_suffix}"
    if engine == "gaussian":
        return f"#p {route}{level_suffix}{geom_suffix}"
    raise InvalidRequestError(f"unsupported engine: {engine}")


def _stage_chk_name(chk_name: str, stage: int) -> str:
    stem = chk_name[:-4] if chk_name.endswith(".chk") else chk_name
    return f"{stem}.{stage}.chk"


def _atom_lines(atoms: tuple[tuple[str, float, float, float], ...]) -> list[str]:
    return [f"{element:<2} {x:20.10f} {y:20.10f} {z:20.10f}" for element, x, y, z in atoms]


def render_gjf(
    path: Path,
    *,
    title: str,
    charge: str,
    multiplicity: str,
    nproc: str,
    mem: str,
    route_line: str,
    chk_name: str,
    fixed_route_line: str,
    final_route_line: str,
    link1_mode: str = "link1",
    multi_stage_chk: bool = False,
) -> str:
    atoms = read_xyz_atoms(path)
    pairs = constraint_pairs(path)
    lines = [f"%nproc={nproc}", f"%mem={mem}"]

    if not pairs or link1_mode == "plain":
        lines.extend([route_line, "", title, "", f"{charge} {multiplicity}", ""])
        lines.extend(_atom_lines(atoms))
        lines.extend(["", ""])
        return "\n".join(lines)

    if link1_mode == "fixed-only":
        lines.extend([fixed_route_line, "", f"{title} fixed opt", "", f"{charge} {multiplicity}"])
        lines.extend(_atom_lines(atoms))
        lines.append("")
        lines.extend(f"B {left} {right} F" for left, right in pairs)
        lines.extend(["", ""])
        return "\n".join(lines)

    fixed_chk = _stage_chk_name(chk_name, 0) if multi_stage_chk else chk_name
    final_chk = _stage_chk_name(chk_name, 1) if multi_stage_chk else chk_name
    lines[0:2] = [f"%nproc={nproc}", f"%mem={mem}"]
    lines.extend([f"%chk={fixed_chk}", fixed_route_line, "", f"{title} fixed opt", ""])
    lines.append(f"{charge} {multiplicity}")
    lines.extend(_atom_lines(atoms))
    lines.append("")
    lines.extend(f"B {left} {right} F" for left, right in pairs)
    lines.extend(["", "--Link1--", f"%nproc={nproc}", f"%mem={mem}"])
    if multi_stage_chk:
        lines.append(f"%oldchk={fixed_chk}")
    lines.extend([f"%chk={final_chk}", final_route_line, ""])
    return "\n".join(lines)


def dpa_json(method: str, model_ref: str, traj_file: str) -> str:
    return json.dumps(
        {"method": method, "model": model_ref, "write_traj": True, "traj_file": traj_file},
        separators=(", ", ": "),
    ) + "\n"


def gaussian_metadata(
    route: str,
    request: DpaBernyRequest,
    traj_file: str,
) -> str:
    return (
        json.dumps(
            {
                "engine": "gaussian",
                "route": route,
                "charge": request.charge,
                "multiplicity": request.multiplicity,
                "nproc": request.nproc,
                "mem": request.mem,
                "functional": request.functional,
                "basis": request.basis,
                "log_file": traj_file,
            },
            separators=(", ", ": "),
        )
        + "\n"
    )


def build_dpa_tasks(request: DpaBernyRequest) -> tuple[DpaTask, ...]:
    xyz_files = expand_xyz_patterns(request.xyz_patterns)
    if not xyz_files:
        raise InvalidRequestError(f"no xyz files matched: {request.xyz_patterns}")
    if request.one_task_per_worker and request.workers < len(xyz_files):
        raise InvalidRequestError(
            "one_task_per_worker requires workers to be at least the number of xyz files"
        )

    used_names: set[str] = set()
    tasks: list[DpaTask] = []
    for index, source_xyz in enumerate(xyz_files):
        task_type = request.task_type
        if task_type == "auto":
            task_type = infer_task_type(source_xyz)
        route = request.route if task_type == "ts" else request.opt_route
        if route is None:
            route = default_route(request.engine, task_type)
        del route  # route is resolved again during rendering; this validates task_type here.

        path_string = str(source_xyz)
        worker = index if request.one_task_per_worker else posix_cksum(path_string) % request.workers
        task_name = f"{sanitize_name(source_xyz.name)}.{short_hash(path_string)}"
        if task_name in used_names:
            task_name = f"{task_name}.{posix_cksum(path_string)}"
        used_names.add(task_name)

        pairs = () if request.ignore_constraints else constraint_pairs(source_xyz)
        if pairs and request.fixed_only:
            json_names = (f"{task_name}.fixed.json",)
            traj_names = (f"{task_name}.fixed.xyz",)
        elif pairs:
            json_names = (f"{task_name}.fixed.json", f"{task_name}.{task_type}.json")
            traj_names = (f"{task_name}.fixed.xyz", f"{task_name}.{task_type}.xyz")
        else:
            json_names = (f"{task_name}.json",)
            traj_names = (f"{task_name}.xyz",)
        tasks.append(
            DpaTask(
                source_xyz=source_xyz,
                worker=worker,
                task_name=task_name,
                gjf_name=f"{task_name}.gjf",
                json_names=json_names,
                traj_names=traj_names,
                task_type=task_type,
            )
        )
    return tuple(tasks)


def render_task_files(request: DpaBernyRequest, task: DpaTask) -> dict[str, str]:
    route = (
        request.opt_route or default_route(request.engine, "opt")
        if task.task_type == "opt"
        else request.route or default_route(request.engine, "ts")
    )
    fixed_route = request.fixed_route
    if fixed_route is None:
        fixed_route = "opt=(modredundant,nomicro)"
    if request.engine == "dpa" and request.fixed_route is None:
        fixed_route = f"{fixed_route} nosymm"
    level_suffix = gaussian_level_suffix(request)
    chk_name = f"{task.task_name}.chk"

    pairs = () if request.ignore_constraints else constraint_pairs(task.source_xyz)
    if pairs and request.fixed_only:
        json_name = task.json_names[0]
        fixed_line = build_route_line(
            request.engine, fixed_route, json_name, level_suffix=level_suffix
        )
        gjf = render_gjf(
            task.source_xyz,
            title=task.task_name,
            charge=request.charge,
            multiplicity=request.multiplicity,
            nproc=request.nproc,
            mem=request.mem,
            route_line=fixed_line,
            chk_name=chk_name,
            fixed_route_line=fixed_line,
            final_route_line=fixed_line,
            link1_mode="fixed-only",
            multi_stage_chk=request.multi_stage_chk,
        )
        route_for_metadata = fixed_line
        return {
            task.gjf_name: gjf,
            json_name: (
                dpa_json(
                    request.method,
                    f"./{request.model_name if request.no_freeze else request.frozen_model}",
                    task.traj_names[0],
                )
                if request.engine == "dpa"
                else gaussian_metadata(route_for_metadata, request, task.traj_names[0])
            ),
        }

    if pairs:
        fixed_json, final_json = task.json_names
        fixed_traj, final_traj = task.traj_names
        fixed_line = build_route_line(
            request.engine, fixed_route, fixed_json, level_suffix=level_suffix
        )
        if task.task_type == "ts":
            geom_suffix = (
                " geom=allcheck"
                if request.engine == "dpa"
                else " geom=(allcheck,PrintInputOrient) guess=tcheck"
            )
        else:
            geom_suffix = " geom=allcheck"
        final_line = build_route_line(
            request.engine,
            route,
            final_json,
            geom_suffix=geom_suffix,
            level_suffix=level_suffix,
        )
        gjf = render_gjf(
            task.source_xyz,
            title=task.task_name,
            charge=request.charge,
            multiplicity=request.multiplicity,
            nproc=request.nproc,
            mem=request.mem,
            route_line=fixed_line,
            chk_name=chk_name,
            fixed_route_line=fixed_line,
            final_route_line=final_line,
            multi_stage_chk=request.multi_stage_chk,
        )
        if request.engine == "dpa":
            model_ref = f"./{request.model_name if request.no_freeze else request.frozen_model}"
            records = {
                fixed_json: dpa_json(request.method, model_ref, fixed_traj),
                final_json: dpa_json(request.method, model_ref, final_traj),
            }
        else:
            records = {
                fixed_json: gaussian_metadata(fixed_line, request, fixed_traj),
                final_json: gaussian_metadata(final_line, request, final_traj),
            }
        return {task.gjf_name: gjf, **records}

    json_name = task.json_names[0]
    plain_line = build_route_line(request.engine, route, json_name, level_suffix=level_suffix)
    gjf = render_gjf(
        task.source_xyz,
        title=task.task_name,
        charge=request.charge,
        multiplicity=request.multiplicity,
        nproc=request.nproc,
        mem=request.mem,
        route_line=plain_line,
        chk_name=chk_name,
        fixed_route_line=plain_line,
        final_route_line=plain_line,
        link1_mode="plain",
        multi_stage_chk=request.multi_stage_chk,
    )
    if request.engine == "dpa":
        model_ref = f"./{request.model_name if request.no_freeze else request.frozen_model}"
        metadata = dpa_json(request.method, model_ref, task.traj_names[0])
    else:
        metadata = gaussian_metadata(plain_line, request, task.traj_names[0])
    return {task.gjf_name: gjf, json_name: metadata}
