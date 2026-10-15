import json
import subprocess
import sys
from pathlib import Path

import pytest
from tricycle_bohrsubmit.models import BatchRequest
from tricycle_bohrsubmit.staging import stage_batch
from tricycle_bohrsubmit.training_protocol import worker_input
from tricycle_core.asset_store import resolve_input
from tricycle_core.io import file_sha256
from tricycle_core.toml import load_toml

from tricycle_sar.preparation import environment, smoke
from tricycle_sar.preparation import inventory as inventory_preparation
from tricycle_sar.sampling_runtime import PACKAGES, stage


def test_smoke_and_worker_preparation_only_need_delivered_packages(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    stage(runtime)
    benchmark = tmp_path / "benchmark/workspace/data"
    benchmark.mkdir(parents=True)
    (benchmark / "sample.csv").write_text("id,value\na,1\n")
    group = tmp_path / "group.toml"
    group.write_text('project_id="test-project"\njob_group_id="test-group"\n')
    paths = [str(runtime / name / "src") for name in PACKAGES]
    code = f"""
import json, sys
from pathlib import Path
sys.path[:0] = {paths!r}
from tricycle_sar.preparation.smoke import prepare
from tricycle_sar.preparation.accelerator_runtime import stage_runtime
from tricycle_sampling.acquisition_worker import prepare_worker
root = Path({str(tmp_path)!r})
request = prepare('rxnresid', root / 'benchmark', root / 'probe', group_config=root / 'group.toml')
prepare_worker(root / 'acquisition')
stage_runtime(root / 'accelerator')
assert 'tools' not in sys.modules and 'analysis' not in sys.modules and 'tricycle_agent' not in sys.modules
assert not (root / 'acquisition/tools').exists()
print(json.dumps({{'request': str(request)}}))
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code], cwd=tmp_path,
                            text=True, capture_output=True, check=True)
    assert Path(json.loads(result.stdout)["request"]).exists()
    assert "tricycle_sampling.acquisition_worker" in (tmp_path / "acquisition/run.sh").read_text()
    subprocess.run(["bash", "-n", str(tmp_path / "acquisition/run.sh")], check=True)
    assert (tmp_path / "accelerator/scripts/sar_accelerator_exec.py").is_file()
    assert (resolve_input(tmp_path / "probe", "common/probe_bundle/scripts/sar_additional_probe.py")).is_file()


def test_inventory_preparation_stages_the_package_diagnostic(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('project_id="test-project"\nimage="test-image"\nmachine_type="test-machine"\n')
    request_path = inventory_preparation.prepare(config, tmp_path / "inventory")
    staged = stage_batch(BatchRequest.from_mapping(load_toml(request_path)),
                         run_id="inventory", workdir=tmp_path, dry_run=True)
    probe = worker_input(staged.run_dir, "worker-000") / "common/inventory.py"
    assert probe.is_file()
    result = subprocess.run([sys.executable, "-I", "-S", str(probe), "--help"],
                            cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "--out-dir" in result.stdout


@pytest.mark.parametrize("probe", [False, True])
def test_smoke_inputs_survive_bohr_task_script_staging(tmp_path: Path, probe: bool) -> None:
    inputs = tmp_path / "inputs"
    group = tmp_path / "group.toml"
    group.write_text('project_id="test-project"\njob_group_id="test-group"\n')
    if probe:
        benchmark = tmp_path / "benchmark"
        data = benchmark / "workspace/data"
        data.mkdir(parents=True)
        (data / "sample.csv").write_text("id,value\na,1\n")
        request = smoke.prepare("rxnresid", benchmark, inputs, group_config=group)
    else:
        request = environment.prepare("rxnresid", inputs, group_config=group)
    staged = stage_batch(
        BatchRequest.from_mapping(load_toml(request)),
        run_id="smoke",
        workdir=tmp_path,
        dry_run=True,
    )
    worker = worker_input(staged.run_dir, "worker-000")
    task = load_toml(worker / (worker / "manifest.txt").read_text().strip())
    hashes = task["input_hashes"]
    assert "__bohr_task_script.sh" in hashes
    assert file_sha256(worker / "common/__bohr_task_script.sh") == file_sha256(
        Path(load_toml(request)["script"])
    )
    for relative, expected in hashes.items():
        assert file_sha256(worker / "common" / relative) == expected
