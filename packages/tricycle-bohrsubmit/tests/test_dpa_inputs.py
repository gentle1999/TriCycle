import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tricycle_bohrsubmit import BohrSubmitPlugin, DpaBernyRequest, InMemoryBohrGateway
from tricycle_bohrsubmit.config import dump_toml, load_toml
from tricycle_bohrsubmit.dpa import render_gjf
from tricycle_bohrsubmit.lifecycle import job_for_row
from tricycle_bohrsubmit.models import RescueRequest
from tricycle_bohrsubmit.rescue import prepare_rescue
from tricycle_bohrsubmit.staging import _chemistry_worker, stage_dpa_berny
from tricycle_core import ExecutionContext
from tricycle_core.errors import InvalidRequestError


def chemistry_request(tmp_path: Path, engine: str, machine: str, **extra: Any) -> DpaBernyRequest:
    xyz = tmp_path / "water.xyz"
    xyz.write_text("3\nwater\nO 0 0 0\nH 0.758 0 0.504\nH -0.758 0 0.504\n")
    model = tmp_path / "model.pth"
    model.write_text("fixture")
    return DpaBernyRequest(
        xyz_patterns=(str(xyz),), workers=1, engine=engine, project_id="123",
        machine_type=machine, task_type="opt", out_dir=tmp_path / "run",
        **({"model_path": model} if engine == "dpa" else {"functional": "HF", "basis": "STO-3G"}),
        **extra,
    )


@pytest.mark.parametrize("engine,machine,valid", [
    ("dpa", "1 * NVIDIA T4_16g", True),
    ("dpa", "c4_m15_1 * NVIDIA T4", True),
    ("dpa", "cpu", False),
    ("dpa", "c16_m64_cpu", False),
    ("dpa", "unspecified", False),
    ("gaussian", "cpu", True),
    ("gaussian", "c16_m64_cpu", True),
    ("gaussian", "1 * NVIDIA T4_16g", False),
    ("gaussian", "cpu-gpu", False),
    ("gaussian", "unspecified", False),
])
def test_chemistry_requires_matching_resource(
    tmp_path: Path, engine: str, machine: str, valid: bool,
) -> None:
    if valid:
        chemistry_request(tmp_path, engine, machine)
    else:
        with pytest.raises(InvalidRequestError, match="requires an explicit"):
            chemistry_request(tmp_path, engine, machine)


@pytest.mark.parametrize("args", [
    ("--machine_type", "cpu"), ("--machine_type=cpu",), ("-t", "cpu"),
    ("-tcpu",), ("--config_file", "job.json"), ("-ijob.json",),
    ("-ytcpu",), ("-yijob.json",),
])
def test_chemistry_rejects_resource_override(tmp_path: Path, args: tuple[str, ...]) -> None:
    with pytest.raises(InvalidRequestError, match="cannot override"):
        chemistry_request(tmp_path, "dpa", "1 * NVIDIA T4_16g", extra_submit_args=args)


@pytest.mark.parametrize("engine,machine", [
    ("dpa", "1 * NVIDIA T4_16g"), ("gaussian", "c16_m64_cpu"),
])
@pytest.mark.parametrize("tamper", ["machine", "extra_args"])
def test_prepared_resource_revalidated_before_side_effect(
    tmp_path: Path, engine: str, machine: str, tamper: str,
) -> None:
    request = chemistry_request(tmp_path, engine, machine)
    staged = stage_dpa_berny(request, run_id="test", workdir=tmp_path, dry_run=True)
    config = load_toml(staged.run_dir / "batch.toml")
    wrong = "cpu" if engine == "dpa" else "1 * NVIDIA T4_16g"
    if tamper == "machine":
        config["bohr"]["machine_type"] = wrong
    else:
        config["bohr"]["extra_submit_args"] = ["-t", wrong]
    dump_toml(staged.run_dir / "batch.toml", config)
    gateway = InMemoryBohrGateway()
    for dry_run in (True, False):
        with pytest.raises(InvalidRequestError):
            BohrSubmitPlugin().submit_prepared(
                staged.run_dir, gateway, ExecutionContext("test", tmp_path, dry_run)
            )
    assert not gateway.groups and not gateway.jobs
    with pytest.raises(InvalidRequestError):
        job_for_row(staged.run_dir, {"worker": "worker-000", "input_dir": str(staged.run_dir)})
    with pytest.raises(InvalidRequestError):
        prepare_rescue(
            RescueRequest(staged.run_dir, 1, allow_missing_downloads=True, out_dir=tmp_path / "rescue"),
            run_id="rescue", workdir=tmp_path,
        )
    assert not (tmp_path / "rescue").exists()


@pytest.mark.parametrize("cuda_available,deepmd_device", [(False, "cpu"), (True, "cpu")])
def test_dpa_worker_stops_before_server_or_gaussian_without_cuda(
    tmp_path: Path, cuda_available: bool, deepmd_device: str,
) -> None:
    # Execute the delivered Bash entrypoint with an unavailable or CPU-only DeepMD runtime.
    # Neither a successful Gaussian exit nor no_freeze may bypass the device requirement.
    (tmp_path / "torch.py").write_text(
        "from types import SimpleNamespace\n"
        f"cuda = SimpleNamespace(is_available=lambda: {cuda_available!r})\n"
    )
    env_module = tmp_path / "deepmd/pt/utils"
    env_module.mkdir(parents=True)
    for package in (env_module, env_module.parent, env_module.parent.parent):
        (package / "__init__.py").touch()
    (env_module / "env.py").write_text(
        f"from types import SimpleNamespace\nDEVICE = SimpleNamespace(type={deepmd_device!r})\n"
    )
    (tmp_path / "dpa-berny.env").write_text("DPA_NO_FREEZE=1\n")
    (tmp_path / "work.sh").write_text(_chemistry_worker("dpa"))
    result = subprocess.run(
        ["bash", "work.sh"], cwd=tmp_path, capture_output=True, text=True, timeout=10,
        env={**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]},
    )
    assert result.returncode != 0
    assert "DPA requires CUDA; CPU fallback is disabled" in result.stderr
    assert not (tmp_path / "server.log").exists()
    assert not (tmp_path / "results/g16-summary.tsv").exists()


@pytest.mark.parametrize("mode", ["plain", "fixed-only", "link1"])
def test_gaussian_molecule_section_contains_xyz_atoms(tmp_path: Path, mode: str) -> None:
    xyz = tmp_path / "water.xyz"
    xyz.write_text("3\n1,2\nO 0 0 0\nH 0.758 0 0.504\nH -0.758 0 0.504\n")
    text = render_gjf(
        xyz,
        title="water",
        charge="0",
        multiplicity="1",
        nproc="1",
        mem="1GB",
        route_line="#p HF/STO-3G opt",
        chk_name="water.chk",
        fixed_route_line="#p HF/STO-3G opt=modredundant",
        final_route_line="#p HF/STO-3G opt geom=allcheck guess=read",
        link1_mode=mode,
    )
    # Gaussian ends the Cartesian molecule at the first blank line after charge/spin.
    lines = text.splitlines()
    molecule = []
    for line in lines[lines.index("0 1") + 1:]:
        if not line.strip():
            break
        molecule.append(line.split())
    assert [atom[0] for atom in molecule] == ["O", "H", "H"]
    assert [[float(v) for v in atom[1:]] for atom in molecule] == [
        [0.0, 0.0, 0.0], [0.758, 0.0, 0.504], [-0.758, 0.0, 0.504],
    ]
