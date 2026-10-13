from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tricycle_core.toml import dump_toml, load_toml
from tricycle_core.training_protocol import file_sha256, resolve_training_registry

from tricycle_sar import get_backend, list_backends
from tricycle_sar.backends.outputs import collect as export_outputs
from tricycle_sar.backends.registry import CATALOG
from tricycle_sar.benchmark import BenchmarkRequest, make_splits, write_rows
from tricycle_sar.modelling_benchmark import prepare_modelling, publish_results
from tricycle_sar.results import read, verify


def inputs(root: Path, count: int) -> tuple[Path, Path, list[dict]]:
    rows: list[dict[str, Any]] = [
        {
            "path_id": f"p{index}",
            "rxn_smiles": "C>>C",
            "G_T_activate": index + 1,
            "reaction_free_energy": 0,
            "ene_id": index,
            "diene_id": 1,
        }
        for index in range(count)
    ]
    dataset = root / "dataset.csv"
    write_rows(dataset, rows)
    request = BenchmarkRequest(
        dataset,
        file_sha256(dataset),
        "G_T_activate",
        "path_id",
        ("ene_id", "diene_id"),
        "rxn_smiles",
        5,
        42,
        0.125,
    )
    splits = root / "splits.csv"
    write_rows(splits, make_splits(rows, request))
    return dataset, splits, rows


def predictions(bundle: Path, fold: int, rows: list[dict]) -> None:
    write_rows(
        bundle / f"model-runs/fingerprint_rf/fold_{fold}/predictions.csv",
        [
            {
                "reaction_id": row["path_id"],
                "row_index": int(row["path_id"][1:]),
                "target_name": "G_T_activate",
                "y_true": row["G_T_activate"] + 1e-7,
                "y_pred": row["G_T_activate"] + 1,
            }
            for row in rows
        ],
    )


def test_package_backends_reuse_smoked_environment_and_run_definitions() -> None:
    assert len(list_backends()) == 11
    assert (
        get_backend("chemprop_cgr").environment
        == get_backend("chemprop_multicomponent").environment
    )
    assert (
        get_backend("fingerprint_rf").method["run"]
        == get_backend("fingerprint_ridge").method["run"]
    )
    assert get_backend("rxnresid").environment["environment_mode"] == "base-venv"
    assert get_backend("rxngraphormer").environment["executor"] == "internal-ssh-docker"
    assert all(not get_backend(model).method["torch_reinstallation"] for model in list_backends())


def test_different_data_sizes_bind_one_parameter_template(tmp_path: Path) -> None:
    backend = get_backend("fingerprint_rf")
    configs = []
    for count in (17, 23):
        root = tmp_path / str(count)
        dataset, splits, _ = inputs(root, count)
        output = root / "prepared"
        backend.prepare(dataset, splits, output, fold=0)
        bundle = output / "common/classical_bundle"
        configs.append(load_toml(bundle / "configs/tricycle-classical.toml"))
        assert configs[-1]["dataset_sha256"] == file_sha256(bundle / configs[-1]["dataset"])
        assert (
            load_toml(output / "tasks/fingerprint_rf-fold-0.toml")["bundle_name"]
            == "classical_bundle"
        )
    assert configs[0]["dataset_sha256"] != configs[1]["dataset_sha256"]
    for config in configs:
        config.pop("dataset_sha256")
        config.pop("splits_sha256")
    assert configs[0] == configs[1]


def test_native_preparation_includes_only_used_adapter_and_initializer(tmp_path: Path) -> None:
    dataset, splits, _ = inputs(tmp_path, 17)
    output = tmp_path / "native"
    get_backend("chemprop_cgr").prepare(dataset, splits, output, fold=0)
    scripts = output / "common/native_bundle/scripts"
    assert sorted(
        p.name for p in (scripts / "tricycle_model_adapters/workflow_adapters").glob("*.py")
    ) == ["__init__.py", "chemprop.py"]
    assert not list((output / "common/sar_model_envs").glob("init_*.sh"))
    assert not (output / "provenance").exists()


def test_provider_and_runtime_integrity_are_checked_before_staging(tmp_path: Path) -> None:
    definition = json.loads(CATALOG.read_text())
    definition["provider"]["sha256"] = "0" * 64
    reference = tmp_path / "catalog.json"
    reference.write_text(json.dumps(definition))
    with pytest.raises(ValueError, match="provider hash"):
        resolve_training_registry(reference)
    definition = json.loads(CATALOG.read_text())
    index_name = definition["integrity"]["path"]
    shutil.copyfile(CATALOG.parent / index_name, tmp_path / index_name)
    origin = __import__("tricycle_core").__file__
    assert origin is not None
    definition["shared_source_roots"] = {"tricycle_core": str(Path(origin).parent)}
    reference.write_text(json.dumps(definition))
    with pytest.raises(ValueError, match="Smoke-validated SAR runtime differs"):
        resolve_training_registry(reference)


def test_compact_outputs_preserve_partial_predictions_and_opt_in_checkpoint(tmp_path: Path) -> None:
    bundle, result = tmp_path / "bundle", tmp_path / "results"
    predictions(
        bundle, 0, [{"path_id": "p0", "G_T_activate": 1}, {"path_id": "p1", "G_T_activate": 2}]
    )
    checkpoint = bundle / "model-runs/fingerprint_rf/fold_0/model.joblib"
    checkpoint.write_bytes(b"checkpoint")
    result.mkdir()
    (result / "install.log").write_text("runtime log")
    task = tmp_path / "task.toml"
    dump_toml(task, {"models": ["fingerprint_rf"], "fold": 0})
    export_outputs(bundle, task, result, 0)
    assert {p.name for p in result.iterdir()} == {"predictions.csv", "metrics.csv", "status.csv"}
    before = (result / "predictions.csv").read_bytes()
    dump_toml(task, {"models": ["fingerprint_rf"], "fold": 1})
    export_outputs(bundle, task, result, 7)
    assert (result / "predictions.csv").read_bytes() == before
    assert read(result / "status.csv")[1]["status"] == "failed"
    dump_toml(task, {"models": ["fingerprint_rf"], "fold": 0, "retain_checkpoint": True})
    export_outputs(bundle, task, result, 0)
    assert (result / "checkpoints/fingerprint_rf/fold_0/model.joblib").read_bytes() == b"checkpoint"


def test_launcher_runs_original_worker_and_compacts_success_and_failure(tmp_path: Path) -> None:
    common = tmp_path / "common"
    common.mkdir()
    task = tmp_path / "task.toml"
    dump_toml(task, {"model": "fingerprint_rf", "fold": 0, "bundle_name": "classical_bundle"})
    predictions(common / "classical_bundle", 0, [{"path_id": "p0", "G_T_activate": 1}])
    shutil.copyfile(CATALOG.parent / "outputs.py", common / "sar_outputs.py")
    worker = common / "sar_model_worker.sh"
    worker.write_text('#!/bin/bash\n[[ "$SAR_COMPACT_OUTPUT" == 1 ]] || exit 9\nexit 0\n')
    launch = CATALOG.parent / "workers/launch.sh"
    result = subprocess.run(
        ["bash", str(launch), str(task)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    assert len(read(tmp_path / "results/predictions.csv")) == 1
    worker.write_text("#!/bin/bash\nexit 7\n")
    result = subprocess.run(
        ["bash", str(launch), str(task)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"},
    )
    assert result.returncode == 7
    assert read(tmp_path / "results/status.csv")[0]["exit_code"] == "7"


def test_compact_downloads_publish_without_recreating_fold_predictions(tmp_path: Path) -> None:
    dataset, splits, rows = inputs(tmp_path, 17)
    config = tmp_path / "experiment.toml"
    dump_toml(
        config,
        {
            "models": ["fingerprint_rf"],
            "dataset": {"path": str(dataset), "sha256": file_sha256(dataset)},
        },
    )
    run = prepare_modelling(config, run_dir=tmp_path / "run")
    assignments = read(splits)
    # Reuse the run's actual deterministic assignments.
    assignments = read(run / "splits.csv")
    bundle = tmp_path / "worker-bundle"
    result = tmp_path / "downloads/worker-000/results"
    task = tmp_path / "task.toml"
    for fold in range(5):
        selected = {
            r["row_id"] for r in assignments if int(r["fold"]) == fold and r["split"] == "test"
        }
        predictions(bundle, fold, [row for row in rows if row["path_id"] in selected])
        dump_toml(task, {"models": ["fingerprint_rf"], "fold": fold})
        export_outputs(bundle, task, result, 0)
    published = publish_results(run, tmp_path / "published", prediction_dir=tmp_path / "downloads")
    assert verify(published)["metrics"]["fingerprint_rf"]["oof_mae"] == 1
    assert not (published / "results/raw").exists()
    assert not (published / "protocol-ref.json").exists()
    assert not list(run.glob("**/fold_*/predictions.csv"))
    assert not (run / "oof-predictions.csv").exists()
    assert not (run / "metrics.toml").exists()


def test_each_model_has_one_current_method_and_no_historical_sources() -> None:
    from tricycle_core.training_protocol import resolve_training_registry

    from tricycle_sar.backends import CATALOG

    definition = json.loads(CATALOG.read_text())
    _, registry = resolve_training_registry(CATALOG)
    assert len(registry["models"]) == len(registry["methods"]) == 11
    assert {model["default_method"] for model in registry["models"].values()} == registry["methods"].keys()
    assert all("variants" not in selection for selection in definition["models"].values())
    assert all("version" not in method and "versions" not in method for method in registry["methods"].values())
    assert not (CATALOG.parent / "archive").exists()
    assert not list(CATALOG.parent.glob("catalog-v*.json"))
    assert not list(CATALOG.parent.glob("SHA256SUMS.v*"))
    assert not list(CATALOG.parent.glob("registry_v*.py"))
    assert not list(CATALOG.parent.glob("execution_v*.py"))
    assert not any("compat" in name for name in [*definition["environments"], *definition["runs"]])
