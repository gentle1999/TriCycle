import sys
import time
import tomllib
from pathlib import Path

import pytest
from tricycle_sar.backends.stage import run_stage


def test_cpu_stage_streams_output_and_checks_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    run_stage(
        [sys.executable, "-c", 'print("actual child output",flush=True)'],
        name="cpu",
        output=tmp_path,
        timeout=5,
        accelerator="cpu",
    )
    assert "actual child output" in (tmp_path / "cpu.log").read_text()
    assert tomllib.loads((tmp_path / "cpu.toml").read_text())["phase"] == "completed"
    with pytest.raises(RuntimeError, match="child exit 7"):
        run_stage(
            [sys.executable, "-c", "raise SystemExit(7)"],
            name="failed",
            output=tmp_path,
            timeout=5,
            accelerator="cpu",
        )


def test_cpu_does_not_accept_visible_cuda(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    with pytest.raises(RuntimeError, match="CUDA_VISIBLE_DEVICES"):
        run_stage(
            [sys.executable, "-c", "pass"],
            name="cpu",
            output=tmp_path,
            timeout=5,
            accelerator="cpu",
        )


def test_stage_timeout_kills_process_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    marker = tmp_path / "should-not-exist"
    child = f"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).touch()"
    parent = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(30)'
    with pytest.raises(TimeoutError):
        run_stage(
            [sys.executable, "-c", parent],
            name="timeout",
            output=tmp_path,
            timeout=0.1,
            accelerator="cpu",
        )
    time.sleep(2.1)
    assert not marker.exists()


def test_missing_or_stale_training_evidence_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    proof = tmp_path / "proof.toml"
    with pytest.raises(RuntimeError, match="without complete accelerator evidence"):
        run_stage(
            [sys.executable, "-c", "pass"],
            name="nop",
            output=tmp_path,
            timeout=5,
            accelerator="cpu",
            evidence=proof,
        )
    proof.write_text("completed = true\nbackward_checked = true\n")
    with pytest.raises(FileExistsError):
        run_stage(
            [sys.executable, "-c", "pass"],
            name="stale",
            output=tmp_path,
            timeout=5,
            accelerator="cpu",
            evidence=proof,
        )


def test_real_optimizer_evidence_and_hook_restoration(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")
    from tricycle_sar.accelerator import AcceleratorGuard

    backward, initialize = torch.autograd.backward, torch.optim.Optimizer.__init__
    model = torch.nn.Linear(2, 1)
    proof = tmp_path / "actual.toml"
    with AcceleratorGuard("cpu", proof, training=True):
        optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
        optimizer.zero_grad()
        model(torch.ones(2, 2)).sum().backward()
        optimizer.step()
    state = tomllib.loads(proof.read_text())
    assert all(
        state[k] for k in ["forward_checked", "backward_checked", "optimizer_checked", "completed"]
    )
    assert torch.autograd.backward is backward
    assert torch.optim.Optimizer.__init__ is initialize
    with (
        pytest.raises(RuntimeError, match="Missing actual model evidence"),
        AcceleratorGuard("cpu", tmp_path / "empty.toml", training=True),
    ):
        pass
    assert torch.autograd.backward is backward
    assert torch.optim.Optimizer.__init__ is initialize


def test_cuda_missing_and_wrong_model_device_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    torch = pytest.importorskip("torch")
    from tricycle_sar.accelerator import AcceleratorGuard

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="CUDA required"):
        AcceleratorGuard("cuda", tmp_path / "cuda.toml", training=True)
    with (
        pytest.raises(RuntimeError, match="Accelerator mismatch"),
        AcceleratorGuard("cpu", tmp_path / "wrong.toml", training=True),
    ):
        torch.nn.Linear(2, 1, device="meta")(torch.ones(1, 2))


def test_staged_launcher_records_real_child_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("torch")
    from tricycle_sar.preparation.accelerator_runtime import stage_runtime

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    stage_runtime(tmp_path)
    script = tmp_path / "tiny_train.py"
    script.write_text("""import torch
model = torch.nn.Linear(2, 1)
optimizer = torch.optim.SGD(model.parameters(), lr=.01)
model(torch.ones(2, 2)).sum().backward()
optimizer.step()
raise SystemExit(0)
""")
    proof = tmp_path / "actual-child.toml"
    run_stage(
        [
            sys.executable,
            str(tmp_path / "scripts/sar_accelerator_exec.py"),
            "--expected",
            "cpu",
            "--report",
            str(proof),
            "--mode",
            "train",
            "--script",
            str(script),
        ],
        name="train",
        output=tmp_path / "stages",
        timeout=30,
        accelerator="cpu",
        evidence=proof,
    )
    state = tomllib.loads(proof.read_text())
    assert state["completed"] and state["optimizer_checked"]
    assert state["optimizer_steps"] == 1
    assert len(state["guard_sha256"]) == 64
