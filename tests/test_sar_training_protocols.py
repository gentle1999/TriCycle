from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from tricycle_bohrsubmit.plugin import BohrSubmitPlugin
from tricycle_bohrsubmit.training_protocol import validate_training_protocol, worker_input
from tricycle_core import ExecutionContext
from tricycle_core.asset_store import resolve_input
from tricycle_core.errors import InvalidRequestError
from tricycle_core.toml import dump_toml, load_toml
from tricycle_core.training_protocol import (
    expand_training_config,
    file_sha256,
    protocol_file_source,
    resolve_training_registry,
    training_method,
)
from tricycle_sar.benchmark import prepare as prepare_benchmark
from tricycle_sar.training import (
    LOCK,
    apply_protocol,
    protocol,
    protocol_file,
    require_benchmark_protocol,
    stage_protocol_bundle,
)

ROOT = Path(__file__).resolve().parents[1]

def test_current_methods_have_verified_full_training_inputs() -> None:
    manifest, registry = resolve_training_registry(LOCK)
    assert len(registry["models"]) == 11
    assert registry["schema"] == "tricycle.sar.backends/v2"
    assert len(registry["methods"]) == 11
    assert "validation_reference" not in registry
    for method_id, method in registry["methods"].items():
        verified = training_method(manifest, registry, method["model"], method_id)
        wrappers = [entry["path"] for entry in verified["files"] if "/init_" in entry["path"]]
        assert not wrappers
        delivered = {entry["path"] for entry in verified["files"]}
        assert "common/sar_model_envs/engine.py" in delivered
        assert "common/sar_model_envs/engine.py" in delivered
        assert "validation" not in verified
    record, _ = protocol("rxngraphormer")
    worker = protocol_file("rxngraphormer", "common/sar_model_worker.sh").read_text()
    assert "sar_execution.py" in worker
    assert (
        "scripts/sar_complete_train_runner.py"
        in protocol_file("rxngraphormer", "common/sar_execution.py").read_text()
    )
    assert record["executor"] == "internal-ssh-docker"
    assert (
        "def run_rxngraphormer("
        in protocol_file(
            "rxngraphormer", "common/training_bundle/scripts/sar_complete_train_runner.py"
        ).read_text()
    )


def staged_classical(tmp_path: Path) -> Any:
    run = tmp_path / "run"
    request_dir = tmp_path / "request"
    request_dir.mkdir()
    bundle = request_dir / "classical_bundle"
    (bundle / "scripts").mkdir(parents=True)
    (bundle / "scripts/unapproved.py").write_text('raise RuntimeError("mutable recipe")')
    (bundle / "data").mkdir()
    (bundle / "data/custom.csv").write_text("one-row-snapshot")
    for relative in (
        "data/snapshots/ground-truth/dataset.csv",
        "data/splits/ground-truth-5fold/assignments.csv",
    ):
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("test input")
    stage_protocol_bundle("fingerprint_rf", bundle)
    assert not (bundle / "scripts/unapproved.py").exists()
    assert (bundle / "data/custom.csv").read_text() == "one-row-snapshot"
    record, _ = protocol("fingerprint_rf")
    request = {
        "image": record["image"],
        "machine_type": record["machine_type"],
        "common_paths": [str(bundle)],
    }
    apply_protocol("fingerprint_rf", request, request_dir, bundle)
    common = run / "worker-000/common"
    shutil.copytree(request_dir / "protocol-runtime", common)
    (common / "worker.sh").rename(common / "__bohr_task_script.sh")
    shutil.copytree(bundle, common / "classical_bundle")
    dump_toml(
        run / "batch.toml",
        {
            "run": {"training_protocol_required": True},
            "bohr": {key: record[key] for key in ("image", "machine_type")},
        },
    )
    from tricycle_bohrsubmit.training_protocol import bind_staged_training_protocol
    bind_staged_training_protocol(
        run, json.loads((request_dir / "training-protocol.json").read_text()), load_toml(run / "batch.toml")
    )
    return run, common


def test_canonical_training_code_drift_rejected_before_remote_side_effect(tmp_path: Path) -> None:
    run, common = staged_classical(tmp_path)
    validate_training_protocol(run)
    (common / "classical_bundle/scripts/tricycle_classical.py").write_text(
        "unapproved training change"
    )
    gateway = Mock()
    with pytest.raises(InvalidRequestError, match="Prepared training method differs"):
        BohrSubmitPlugin().submit_prepared(run, gateway, ExecutionContext("test", tmp_path, False))
    assert not gateway.mock_calls


def test_generated_configuration_cannot_change_validated_parameters(tmp_path: Path) -> None:
    run, common = staged_classical(tmp_path)
    path = common / "classical_bundle/configs/tricycle-classical.toml"
    value = load_toml(path)
    value["models"]["fingerprint_rf"]["n_estimators"] = 1
    dump_toml(path, value)
    with pytest.raises(InvalidRequestError, match="Generated training configuration differs"):
        validate_training_protocol(run)


def test_current_reference_validates_manifest_hash(tmp_path: Path) -> None:
    original = tmp_path / "original.json"
    original.write_text(json.dumps({"models": {"model": {"image": "verified"}}}))
    reference = tmp_path / "redirect.json"
    reference.write_text(json.dumps({"manifest": "original.json", "sha256": file_sha256(original)}))
    manifest, registry = resolve_training_registry(reference, file_sha256(original))
    assert file_sha256(manifest) == file_sha256(original)
    assert "methods" not in registry
    with pytest.raises(ValueError, match="registry hash differs"):
        resolve_training_registry(reference, "0" * 64)


def test_experiment_cannot_override_locked_hyperparameters() -> None:
    config = ROOT / "configs/example-sar-benchmark.toml"
    value = load_toml(config)
    assert "modelling" not in value
    value["modelling"] = {"training_seed": 123}
    with pytest.raises(ValueError, match="must come from the protocol"):
        expand_training_config(config, value)


def test_new_dataset_size_is_a_binding_and_protocol_is_recorded(tmp_path: Path) -> None:
    data = tmp_path / "dataset.csv"
    with data.open("w", newline="") as stream:
        fields = [
            "path_id",
            "rxn_smiles",
            "G_T_activate",
            "ene_id",
            "diene_id",
            "reaction_free_energy",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for index in range(12):
            writer.writerow(
                dict(zip(fields, [f"p{index}", "C>>C", index + 1, str(index), "1", 0], strict=True))
            )
    config = tmp_path / "experiment.toml"
    dump_toml(
        config,
        {
            "experiment_id": "new-size",
            "models": ["fingerprint_rf"],
            "training_protocol": {"reference": str(LOCK)},
            "dataset": {
                "path": str(data),
                "sha256": file_sha256(data),
                "expected_rows": 12,
                "expected_groups": 12,
            },
        },
    )
    run = prepare_benchmark(config, tmp_path / "benchmark")
    state = load_toml(run / "run.toml")
    assert state["rows"] == 12
    manifest, _ = resolve_training_registry(LOCK)
    assert state["training_protocol_manifest_sha256"] == file_sha256(manifest)
    require_benchmark_protocol(run)
    state["training_protocol_manifest_sha256"] = "0" * 64
    dump_toml(run / "run.toml", state)
    with pytest.raises(ValueError, match="different training backend"):
        require_benchmark_protocol(run)


def test_tampered_protocol_file_source_is_rejected(tmp_path: Path) -> None:
    manifest, registry = resolve_training_registry(LOCK)
    method = training_method(manifest, registry, "fingerprint_rf")
    for entry in method["files"]:
        if not entry["external"]:
            target = tmp_path / entry["source"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(protocol_file_source(manifest, entry), target)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(registry))
    (tmp_path / method["files"][0]["source"]).write_bytes(b"unapproved code")
    with pytest.raises(ValueError, match="Protocol file hash differs"):
        training_method(path, registry, "fingerprint_rf")


@pytest.mark.parametrize("copy_fallback", [False, True])
def test_batch_reuses_common_files_and_binds_inputs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, copy_fallback: Any
) -> None:
    from tricycle_bohrsubmit.models import BatchRequest
    from tricycle_bohrsubmit.staging import stage_batch

    if copy_fallback:

        def unavailable(*args: Any) -> None:
            raise OSError("hard links unavailable")

        monkeypatch.setattr("tricycle_bohrsubmit.staging.os.link", unavailable)
    staged_run, staged_common = staged_classical(tmp_path)
    common = tmp_path / "batch-input-common"
    shutil.copytree(staged_common, common)
    bound = json.loads((staged_run / "training-protocol.json").read_text())
    (common / "training-protocol.json").write_text(json.dumps({
        key: bound[key] for key in ("model", "registry", "registry_sha256", "method_id")
    }))
    task = tmp_path / "fold.toml"
    task.write_text('models = ["fingerprint_rf"]\nfold = 0\nsmoke = false\n')
    record, _ = protocol("fingerprint_rf")
    run = tmp_path / "batch"
    request = BatchRequest(
        task_patterns=(str(task),),
        workers=2,
        script=common / "__bohr_task_script.sh",
        common_paths=tuple(p for p in common.iterdir() if p.name != "__bohr_task_script.sh"),
        image=record["image"],
        machine_type=record["machine_type"],
        project_id="123",
        out_dir=run,
    )
    stage_batch(request, run_id="shared", workdir=tmp_path, dry_run=True)
    validate_training_protocol(run)
    marker = json.loads((run / "training-protocol.json").read_text())
    assert "method_files" not in marker
    assert marker["common_files"] == {
        "common/classical_bundle/data/custom.csv": file_sha256(
            common / "classical_bundle/data/custom.csv"
        ),
        **{
            f"common/classical_bundle/{relative}": file_sha256(
                common / "classical_bundle" / relative
            )
            for relative in (
                "data/snapshots/ground-truth/dataset.csv",
                "data/splits/ground-truth-5fold/assignments.csv",
                "configs/tricycle-classical.toml",
            )
        },
    }
    assert not list(run.glob("worker-*/common/training-protocol.json"))
    a, b = [worker_input(run, f"worker-{i:03d}") / "common/classical_bundle/data/custom.csv" for i in range(2)]
    assert a.read_bytes() == b.read_bytes()
    if not copy_fallback:
        assert a.stat().st_ino == b.stat().st_ino
    assert a.stat().st_ino != (common / "classical_bundle/data/custom.csv").stat().st_ino
    # Shared inputs remain independent of the source and are checked before remote effects.
    assert not a.stat().st_mode & 0o222
    a.chmod(0o644)  # Deliberate corruption is confined to this test's asset store.
    a.write_text("changed data")
    assert (common / "classical_bundle/data/custom.csv").read_text() == "one-row-snapshot"
    gateway = Mock()
    with pytest.raises(InvalidRequestError, match="Shared worker input assets rejected"):
        BohrSubmitPlugin().submit_prepared(
            run, gateway, ExecutionContext("submit", tmp_path, False)
        )
    assert not gateway.mock_calls


def test_internal_method_cannot_be_prepared_as_bohr(tmp_path: Path) -> None:
    from tricycle_sar.preparation.additional import prepare

    output = tmp_path / "should-not-exist"
    with pytest.raises(ValueError, match="pinned internal executor"):
        prepare("rxngraphormer", tmp_path / "benchmark", output, group_name="test")
    assert not output.exists()


def test_additional_preparation_stages_only_pinned_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tricycle_sar.preparation.additional import prepare

    benchmark = tmp_path / "benchmark"
    data = benchmark / "workspace/data/snapshots/ground-truth/dataset.csv"
    data.parent.mkdir(parents=True)
    data.write_text("path_id,rxn_smiles,G_T_activate\np0,C>>C,1\n")
    (benchmark / "splits.csv").write_text("fold,row_id,split\n0,p0,train\n")
    dump_toml(benchmark / "run.toml", {"dataset_sha256": file_sha256(data)})
    monkeypatch.setattr("tricycle_sar.preparation.additional.check", lambda run: None)
    directory = tmp_path / "rxnresid"
    prepare("rxnresid", benchmark, directory, group_name="test", project_id="123")
    request = load_toml(directory / "bohr-train.toml")
    record, _ = protocol("rxnresid")
    assert request["image"] == record["image"]
    assert request["machine_type"] == record["machine_type"]
    assert not (directory / "bundle-manifest.json").exists()
    assert not (directory / "rxnresid.tar.gz").exists()
    assert not list(directory.rglob("init_*.sh"))
    for entry in record["files"]:
        relative = entry["path"].removeprefix("common/")
        source = (
            resolve_input(directory, relative)
            if relative.startswith("training_bundle/")
            else resolve_input(directory, "protocol-runtime")
            / ("worker.sh" if relative == "__bohr_task_script.sh" else relative)
        )
        assert file_sha256(source) == entry["sha256"]


def test_full_plan_balances_bohr_tasks_and_keeps_internal_executor_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections import Counter

    from tricycle_bohrsubmit.models import BatchRequest
    from tricycle_bohrsubmit.staging import stage_batch
    from tricycle_sar.preparation import all_models as assembler

    benchmark = tmp_path / "benchmark"
    config = benchmark / "workspace/configs/tricycle-reactiont5.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"checkpoint_directory": "assets/reactiont5"}))
    from tricycle_sar import list_backends

    dump_toml(benchmark / "run.toml", {"models": list(list_backends())})
    monkeypatch.setattr(assembler, "check", lambda run: None)
    monkeypatch.setattr(assembler, "require_reactiont5_assets", lambda assets, spec: None)

    def component(directory: Any, model: Any, prefix: Any, count: Any) -> None:
        directory.mkdir(parents=True)
        record, _ = protocol("fingerprint_rf" if model == "classical" else model)
        dump_toml(
            directory / "bohr-train.toml",
            {
                "task_defaults": {"model": model, "smoke": False},
                "task_specs": [{"name": f"{prefix}{fold}", "fold": fold} for fold in range(count)],
                "image": record["image"],
                "machine_type": record["machine_type"],
                "workers": 1,
                "script": str(protocol_file("fingerprint_rf", "common/__bohr_task_script.sh")),
                "project_id": "123",
            },
        )

    monkeypatch.setattr(
        assembler,
        "prepare_classical",
        lambda run, out, **kwargs: component(out, "classical", "fingerprint_", 15),
    )

    def molecular_stub(run: Path, out: Path, **kwargs: Any) -> None:
        for model in ("uam", "molclr"):
            component(out / model, model, "train-", 5)

    def native_stub(run: Path, out: Path, **kwargs: Any) -> None:
        for model in ("chemprop_cgr", "chemprop_multicomponent", "t5chem"):
            component(out / model, model, "train-", 5)

    monkeypatch.setattr(assembler, "prepare_molecular", molecular_stub)
    monkeypatch.setattr(assembler, "prepare_native", native_stub)

    def additional(model: Any, run: Any, out: Any, **kwargs: Any) -> None:
        assert model in ("rxnresid", "reactiont5")
        component(out, model, "fold-", 5)

    monkeypatch.setattr(assembler, "prepare_additional", additional)
    group = tmp_path / "group.toml"
    dump_toml(group, {"project_id": "123", "job_group_id": "test-group"})
    output = assembler.prepare(benchmark, tmp_path / "requests", group_name="test", group_config=group)
    plan = load_toml(output / "submit-order.toml")
    bohr = [entry for entry in plan["models"] if entry["executor"] == "bohr"]
    assert sum(entry["fold_tasks"] for entry in bohr) == 50
    internal = [entry for entry in plan["models"] if entry["executor"] != "bohr"]
    assert len(internal) == 1 and internal[0]["name"] == "rxngraphormer"
    assert "request" not in internal[0]
    assert internal[0]["method_id"] == protocol("rxngraphormer")[0]["method_id"]
    for entry in bohr:
        request = load_toml(Path(entry["request"]))
        batch = stage_batch(
            BatchRequest.from_mapping(
                {**request, "out_dir": str(tmp_path / f"staged-{entry['name']}")}
            ),
            run_id=entry["name"],
            workdir=tmp_path,
            dry_run=True,
        )
        assigned = Counter(batch.worker_task_counts)
        assert len(assigned) == 1
        assert batch.task_count == entry["fold_tasks"]
        assert request["job_group_name"] == "test"


def test_historical_registry_candidates_are_rejected(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    current.write_text(json.dumps({"models": {}}))
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps({"models": {"old": {}}}))
    reference = tmp_path / "reference.json"
    reference.write_text(json.dumps({
        "manifest": current.name,
        "sha256": file_sha256(current),
        "compatible_manifests": [{"manifest": previous.name, "sha256": file_sha256(previous)}],
    }))
    for expected in (None, file_sha256(current), file_sha256(previous)):
        with pytest.raises(ValueError, match="Historical training registries are not supported"):
            resolve_training_registry(reference, expected)


def test_provider_hash_mismatch_never_executes_neighboring_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tricycle_core.training_protocol as resolver

    installed = tmp_path / "installed"
    installed.mkdir()
    module = installed / "sar_test_provider.py"
    module.write_text("def load_registry(reference, value): return {}\n")
    monkeypatch.syspath_prepend(str(installed))
    marker = tmp_path / "executed"
    neighboring = tmp_path / module.name
    neighboring.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "def load_registry(reference, value): return {}\n"
    )
    reference = tmp_path / "catalog.json"
    reference.write_text(json.dumps({"provider": {
        "entrypoint": "sar_test_provider:load_registry",
        "sha256": file_sha256(neighboring),
    }}))
    with pytest.raises(ValueError, match="provider hash differs"):
        resolver.resolve_training_registry(reference)
    assert not marker.exists()


def test_per_worker_protocol_record_is_not_a_submission_fallback(tmp_path: Path) -> None:
    run, common = staged_classical(tmp_path)
    marker = run / "training-protocol.json"
    shutil.copyfile(marker, common / marker.name)
    marker.unlink()
    gateway = Mock()
    with pytest.raises(InvalidRequestError, match="Per-worker training protocol"):
        BohrSubmitPlugin().submit_prepared(run, gateway, ExecutionContext("old", tmp_path, False))
    assert not gateway.mock_calls
