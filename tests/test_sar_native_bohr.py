from pathlib import Path
from unittest.mock import patch

import pytest
from tricycle_bohrsubmit.models import BatchRequest
from tricycle_bohrsubmit.staging import stage_batch
from tricycle_bohrsubmit.task_table import expand_task_specs
from tricycle_core.toml import dump_toml, load_toml
from tricycle_sar.backends.native_runner import validate_task
from tricycle_sar.preparation.native import prepare


def test_shared_task_configuration_expands_for_pinned_workers_and_rejects_drift(
    tmp_path: Path,
) -> None:
    run = tmp_path / "benchmark"
    data = run / "workspace/data/input.csv"
    data.parent.mkdir(parents=True)
    data.write_text("fixture")
    dump_toml(run / "run.toml", {"models": ["chemprop_cgr", "chemprop_multicomponent", "t5chem"]})
    output = tmp_path / "requests"
    group_config = tmp_path / "group.toml"
    dump_toml(group_config, {"project_id": "test-project", "job_group_id": "test-group"})
    with (
        patch("tricycle_sar.preparation.native.check"),
        patch("subprocess.run", side_effect=AssertionError("network")),
    ):
        prepare(run, output, group_config=group_config)
    assert not list(output.glob("*/tasks/*.toml"))
    assert not list(output.glob("*/bohr-smoke.toml"))
    requests = sorted(output.glob("*/bohr-train.toml"))
    assert len(requests) == 3
    for index, path in enumerate(requests):
        value = load_toml(path)
        assert len(value["task_defaults"]["hashes"]) > 0
        assert all(set(row) == {"name", "fold"} for row in value["task_specs"])
        bundle = path.parent / "native_bundle"
        staged = stage_batch(
            BatchRequest.from_mapping({**value, "out_dir": str(tmp_path / f"batch-{index}")}),
            run_id=f"model-{index}",
            workdir=tmp_path,
            dry_run=True,
        )
        assert staged.task_count == 5
        assert staged.worker_task_counts == (1, 1, 1, 1, 1)
        expanded = list(staged.run_dir.glob("worker-*/tasks/*.toml"))
        assert len(expanded) == 5
        for task in expanded:
            validate_task(load_toml(task), bundle)
        task_record = expand_task_specs(value["task_defaults"], value["task_specs"])[0][1]
        (bundle / task_record["hashes"][0]["path"]).write_text("tampered")
        with pytest.raises(ValueError, match="checksum mismatch"):
            validate_task(task_record, bundle)
