import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tricycle_core.training_protocol import protocol_file_source

from tricycle_sar import get_backend, list_backends
from tricycle_sar.backends.execution import training_command
from tricycle_sar.backends.registry import CATALOG
from tricycle_sar.predictions import aggregate_pairs, validate_rows
from tricycle_sar.preparation.demo import validate_sampling_dataset
from tricycle_sar.results import metrics


def test_constant_labels_use_shared_metrics_in_worker_and_publication() -> None:
    from tricycle_sampling.replay.worker import metrics as worker_metrics

    assert metrics([{"y_true": "2", "y_pred": "3"}]) == {"mae": 1, "rmse": 1}
    assert worker_metrics([{"target": "2", "prediction": "3"}])["mae"] == 1


@pytest.mark.parametrize(
    "field,value", [("prediction", "nan"), ("epistemic_variance", "-1"), ("predictive_std", "-1")]
)
def test_prediction_signals_reject_invalid_numbers(field: Any, value: Any) -> None:
    row = {
        "ene_id": "1",
        "diene_id": "2",
        "path_id": "path-a",
        "prediction": "3",
        "epistemic_variance": "1",
        "aleatoric_variance": "2",
        "predictive_variance": "3",
        field: value,
    }
    with pytest.raises(ValueError):
        validate_rows([row])


def test_pair_aggregation_preserves_multiplicity_and_exact_coverage() -> None:
    first = {
        "ene_id": "1",
        "diene_id": "2",
        "path_id": "path-a",
        "prediction": "3",
        "epistemic_variance": "1",
        "aleatoric_variance": "2",
        "predictive_variance": "3",
    }
    second = {**first, "path_id": "path-b", "prediction": "5"}
    validate_rows([first, second], expected=[first, second])
    assert aggregate_pairs([first, second])["1+2"] == {
        "prediction_mean": 4,
        "product_count": 2,
        "epistemic_variance": 2,
        "aleatoric_variance": 4,
        "predictive_variance": 6,
    }
    with pytest.raises(ValueError, match="coverage"):
        validate_rows([first], expected=[first, second])
    with pytest.raises(ValueError, match="duplicate"):
        validate_rows([first, first], unique=True)


@pytest.mark.parametrize("model", list_backends())
def test_staged_initializers_load_offline_without_workspace_or_installed_packages(
    tmp_path: Path, model: Any
) -> None:
    backend = get_backend(model)
    for entry in backend.method["files"]:
        if entry["path"].startswith(("common/sar_model_envs/", "common/sar_runtime/")):
            target = tmp_path / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(protocol_file_source(CATALOG, entry), target)
    environment_model = (
        "classical"
        if model.startswith("fingerprint_")
        else "chemprop"
        if model.startswith("chemprop_")
        else model
    )
    out = tmp_path / "not-created"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(tmp_path / "common/sar_model_envs/engine.py"),
            "--model",
            environment_model,
            "--root",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    plan = json.loads(result.stdout)
    assert plan["status"] == "plan"
    assert not plan["training_started"]
    assert not out.exists()
    bundle = backend.method["bundle_name"]
    command = training_command(
        {
            "model": model,
            "fold": 3,
            "bundle_name": bundle,
            "smoke": False,
            "task_path": "/task.toml",
        },
        "/env/python",
        tmp_path / "results",
    )
    assert command[0] == "/env/python"
    if bundle != "native_bundle":
        assert command[command.index("--fold") + 1] == "3"
    else:
        assert command[-2:] == ["--task", "/task.toml"]
    assert (
        next(e for e in backend.method["files"] if e["path"] == "common/sar_model_worker.sh")[
            "source"
        ]
        == "workers/unified.sh"
    )


def test_sampling_demo_requires_explicit_ids_without_guessing(tmp_path: Path) -> None:
    path = tmp_path / "raw.csv"
    path.write_text("mapped_reaction_smiles,activation_gibbs_free_energy_kcal_mol\nC>>C,2\n")
    with pytest.raises(ValueError, match="experiment columns"):
        validate_sampling_dataset(path)


def test_environment_preparation_delivers_shared_runtime_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    from tricycle_core.io import file_sha256
    from tricycle_core.toml import load_toml

    from tricycle_sar.backends.execution import execute
    from tricycle_sar.preparation.environment import prepare

    output = tmp_path / "environment"
    group = tmp_path / "group.toml"
    group.write_text('project_id="test-project"\njob_group_id="test-group"\n')
    request = prepare("rxnresid", output, group_config=group)
    assert request.is_file()
    task = load_toml(output / "task.toml")
    assert task["environment_model"] == "rxnresid"
    assert task["input_hashes"]["sar_model_envs/engine.py"] == file_sha256(
        output / "common/sar_model_envs/engine.py"
    )
    (output / "common/sar_model_envs/engine.py").write_text("modified")
    with pytest.raises(ValueError, match="input hash mismatch"):
        execute(output / "task.toml", output / "common", output / "results", initialize_only=True)
    assert not (output / "results").exists()


def test_published_data_is_prepared_with_current_backend(
    tmp_path: Path, result_package_factory: Any
) -> None:
    from tricycle_core.toml import load_toml

    from tricycle_sar.records import read_method_bindings
    from tricycle_sar.results import prepare_training

    published = result_package_factory("rxnresid", recorded=True)
    result = prepare_training(published, "rxnresid", 0, tmp_path / "rerun")
    assert result["method_id"] == get_backend("rxnresid").method["method_id"]
    recorded = read_method_bindings(published / "METHOD_BINDINGS.csv")["rxnresid", 0]
    assert result["method_id"] != recorded
    task = load_toml(Path(result["input_dir"]) / result["task"])
    assert task["bundle_name"] == "training_bundle"
    assert task["environment_model"] == "rxnresid"
    assert not result["submitted"]


def test_sampling_demo_accepts_local_data_without_database_metadata(tmp_path: Path) -> None:
    path = tmp_path / "local.csv"
    path.write_text(
        "path_id,rxn_smiles,ene_id,diene_id,prod_id,G_T_activate\nlocal-path,[C:1]>>[C:1],local-a,local-b,route-a,2\n"
    )
    rows = validate_sampling_dataset(path)
    assert rows[0]["ene_id"] == "local-a"
    assert rows[0]["path_id"] == "local-path"
