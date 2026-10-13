import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from tricycle_bohrsubmit.plugin import BohrSubmitPlugin
from tricycle_bohrsubmit.training_protocol import validate_training_protocol
from tricycle_core import ExecutionContext
from tricycle_core.errors import InvalidRequestError
from tricycle_core.toml import dump_toml
from tricycle_core.training_protocol import method_fingerprint


def fixture_protocol(tmp_path: Path, model: str = "test") -> Any:
    registry = tmp_path / "registry.json"
    runtime = tmp_path / "entry.sh"
    runtime.write_text("#!/bin/sh\nexit 0\n")
    entry = {"path": "common/__bohr_task_script.sh", "source": "entry.sh",
             "sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
             "bytes": runtime.stat().st_size, "executable": False, "external": False}
    fingerprint = method_fingerprint(model, [entry])
    method_id = f"{model}/train/{fingerprint[:16]}"
    registry.write_text(json.dumps({"models": {model: {"default_method": method_id}},
        "methods": {method_id: {"model": model, "mode": "train", "executor": "bohr",
            "image": "test", "machine_type": "cpu", "files": [entry], "fingerprint": fingerprint}}}))
    run = tmp_path / "run"
    run.mkdir()
    common = run / "worker-000/common"
    common.mkdir(parents=True)
    (common / "__bohr_task_script.sh").write_bytes(runtime.read_bytes())
    source = run / "worker.py"
    source.write_text("original successful worker")
    marker = run / "training-protocol.json"
    marker.write_text(
        json.dumps(
            {
                "model": model,
                "method_id": method_id,
                "workers": ["worker-000"],
                "common_files": {},
                "registry": str(registry),
                "registry_sha256": hashlib.sha256(registry.read_bytes()).hexdigest(),
                "files": {"worker.py": hashlib.sha256(source.read_bytes()).hexdigest()},
            }
        )
    )
    dump_toml(
        run / "batch.toml",
        {
            "bohr": {"image": "test", "machine_type": "cpu"},
            "run": {
                "training_protocol_required": True,
                "training_protocol_sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
            }
        },
    )
    return run, source, marker


def test_changed_worker_is_rejected_before_remote_side_effect(tmp_path: Path) -> None:
    run, source, _ = fixture_protocol(tmp_path)
    validate_training_protocol(run)
    source.write_text("unapproved environment installation")
    gateway = Mock()
    with pytest.raises(InvalidRequestError, match="Prepared training input differs"):
        BohrSubmitPlugin().submit_prepared(run, gateway, ExecutionContext("test", tmp_path, False))
    assert not gateway.mock_calls


def test_missing_marker_cannot_disable_protocol(tmp_path: Path) -> None:
    run, _, marker = fixture_protocol(tmp_path)
    marker.unlink()
    with pytest.raises(InvalidRequestError, match="protocol is missing"):
        validate_training_protocol(run)


def test_modified_hash_manifest_cannot_bless_worker_changes(tmp_path: Path) -> None:
    run, source, marker = fixture_protocol(tmp_path)
    source.write_text("changed")
    data = json.loads(marker.read_text())
    data["files"]["worker.py"] = hashlib.sha256(source.read_bytes()).hexdigest()
    marker.write_text(json.dumps(data))
    with pytest.raises(InvalidRequestError, match="marker changed"):
        validate_training_protocol(run)


def reactiont5_protocol(tmp_path: Path) -> Any:
    run, _, _ = fixture_protocol(tmp_path, "reactiont5")
    bundle = run / "worker-000/common/training_bundle"
    (bundle / "assets").mkdir(parents=True)
    weight = b"pinned test fixture"
    (bundle / "reactiont5.json").write_text(
        json.dumps(
            {"files": [{"path": "pytorch_model.bin", "sha256": hashlib.sha256(weight).hexdigest()}]}
        )
    )
    return run, bundle, weight


def test_reactiont5_missing_weight_is_rejected_before_submit(tmp_path: Path) -> None:
    run, _, _ = reactiont5_protocol(tmp_path)
    gateway = Mock()
    with pytest.raises(InvalidRequestError, match="local asset missing"):
        BohrSubmitPlugin().submit_prepared(run, gateway, ExecutionContext("test", tmp_path, False))
    assert not gateway.mock_calls


def test_reactiont5_wrong_checkpoint_is_rejected(tmp_path: Path) -> None:
    run, bundle, _ = reactiont5_protocol(tmp_path)
    (bundle / "assets/pytorch_model.bin").write_bytes(b"wrong revision")
    with pytest.raises(InvalidRequestError, match="asset hash differs"):
        validate_training_protocol(run)


def test_reactiont5_verified_weight_can_submit(tmp_path: Path) -> None:
    run, bundle, weight = reactiont5_protocol(tmp_path)
    (bundle / "assets/pytorch_model.bin").write_bytes(weight)
    validate_training_protocol(run)


def test_interrupted_retry_keeps_later_job_receipts_and_never_submits_them_twice(
    tmp_path: Path,
) -> None:
    import csv

    from tricycle_bohrsubmit.models import BatchRequest
    from tricycle_bohrsubmit.staging import stage_batch

    task = tmp_path / "task.toml"
    task.write_text("fold = 0\n")
    script = tmp_path / "worker.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    run = tmp_path / "batch"
    stage_batch(
        BatchRequest(
            task_patterns=(str(task),),
            workers=2,
            script=script,
            project_id="1",
            machine_type="cpu",
            job_group_id="known",
            auto_job_group=False,
            out_dir=run,
        ),
        run_id="retry",
        workdir=tmp_path,
        dry_run=True,
    )
    plan = run / "submit-plan.tsv"
    rows = list(csv.DictReader(plan.open(), delimiter="\t"))
    for row in rows:
        row["tasks"] = "1"
    with plan.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys(), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    # A prior attempt accepted worker 1; it must survive worker 0's retry failure.
    (run / "submit-results.tsv").write_text(
        "worker\ttasks\tjob_id\tjob_name\tstatus\tinput_dir\n"
        f"worker-001\t1\taccepted-1\tknown\tFAILED\t{run / 'worker-001'}\n"
    )
    gateway = Mock()
    gateway.submit.side_effect = RuntimeError("connection failed before acceptance")
    context = ExecutionContext("retry", tmp_path, False)
    with pytest.raises(RuntimeError, match="connection failed"):
        BohrSubmitPlugin().submit_prepared(run, gateway, context)
    saved = {
        row["worker"]: row
        for row in csv.DictReader((run / "submit-results.tsv").open(), delimiter="\t")
    }
    assert saved["worker-001"]["job_id"] == "accepted-1"
    gateway.submit.side_effect = None
    gateway.submit.return_value = "accepted-0"
    gateway.reset_mock()
    result = BohrSubmitPlugin().submit_prepared(run, gateway, context)
    assert result.metadata["submitted"] == 1
    assert result.metadata["reused"] == 1
    assert gateway.submit.call_count == 1
