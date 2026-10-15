from pathlib import Path
from typing import Any

import pytest
from tricycle_bohrsubmit.models import BatchRequest
from tricycle_bohrsubmit.plugin import BohrSubmitPlugin
from tricycle_bohrsubmit.staging import stage_batch
from tricycle_bohrsubmit.task_table import expand_task_specs
from tricycle_bohrsubmit.training_protocol import worker_input
from tricycle_core import ExecutionContext
from tricycle_core.errors import InvalidRequestError
from tricycle_core.toml import dump_toml, load_toml


@pytest.mark.parametrize(
    "row",
    [
        {"name": "../escape", "fold": 0},
        {"name": "bad\nname", "fold": 0},
        {"name": "fold-0", "model": "changed"},
    ],
)
def test_bad_task_differences_rejected_before_output(tmp_path: Path, row: Any) -> None:
    with pytest.raises(InvalidRequestError):
        request = BatchRequest.from_mapping(
            {
                "task_defaults": {"model": "fixed"},
                "task_specs": [row],
                "script": "unused.sh",
                "workers": 1,
                "project_id": "1",
                "machine_type": "cpu",
            }
        )
        BohrSubmitPlugin().prepare(
            "batch.prepare", request.to_dict(), ExecutionContext("invalid", tmp_path)
        )
    assert not (tmp_path / "runs").exists()


def test_roundtrip_expansion_preserves_shared_hashes_seed_and_balanced_assignment(
    tmp_path: Path,
) -> None:
    script = tmp_path / "worker.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    defaults = {
        "model": "fixed",
        "smoke": False,
        "hashes": [{"path": "data.csv", "sha256": "a" * 64}],
    }
    rows = [{"name": f"seed-{seed}", "fold": 0, "seed": seed} for seed in range(3)]
    config = tmp_path / "request.toml"
    dump_toml(
        config,
        {
            "task_defaults": defaults,
            "task_specs": rows,
            "workers": 3,
            "script": str(script),
            "project_id": "1",
            "machine_type": "cpu",
            "backward_files": "worker.log,results,manifest.txt",
        },
    )
    request = BatchRequest.from_mapping(load_toml(config))
    copied = BatchRequest.from_mapping(request.to_dict())
    assert expand_task_specs(copied.task_defaults, copied.task_specs) == expand_task_specs(
        defaults, rows
    )
    staged = stage_batch(copied, run_id="seeds", workdir=tmp_path, dry_run=True)
    assert staged.worker_task_counts == (1, 1, 1)
    assert not list(staged.run_dir.glob("worker-*/manifest.tsv"))
    tasks = [task for worker in sorted(staged.run_dir.glob("worker-*"))
             for task in worker_input(staged.run_dir, worker.name).glob("tasks/*.toml")]
    assert len(tasks) == 3
    assert not list(staged.run_dir.glob("worker-*/tasks"))
    for seed, task in enumerate(tasks):
        assert load_toml(task) == {**defaults, "fold": 0, "seed": seed}
    assert (staged.run_dir / "tasks.tsv").is_file()


def test_repeated_task_names_and_mixed_sources_are_rejected() -> None:
    with pytest.raises(InvalidRequestError, match="Duplicate task name"):
        expand_task_specs({}, [{"name": "fold-0"}, {"name": "fold-0"}])
    with pytest.raises(InvalidRequestError, match="mutually exclusive"):
        BatchRequest.from_mapping(
            {
                "task_patterns": ["old.toml"],
                "task_specs": [{"name": "new"}],
                "workers": 1,
                "script": "worker.sh",
                "project_id": "1",
                "machine_type": "cpu",
            }
        )
