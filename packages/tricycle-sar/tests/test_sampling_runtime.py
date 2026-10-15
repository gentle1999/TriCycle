from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tricycle_sar import (
    build_finetune_command,
    build_inference_command,
    build_training_command,
    get_backend,
    load_model_asset,
)
from tricycle_sar.rxnresid import freeze_route_vocabulary, route_command
from tricycle_sar.sampling_runtime import PACKAGES, initialize, stage, validate_source_spec, verify


def test_backend_uses_public_commands_for_train_refit_and_prediction() -> None:
    backend = get_backend("rxnresid")
    common: dict[str, Any] = {
        "epochs": 300,
        "batch_size": 512,
        "device": "auto",
        "mixed_precision": "no",
    }
    assert backend.training_command(fold_index=0, seed=42, **common) == build_training_command(
        fold_index=0, seed=42, **common
    )
    command = backend.refit_command(checkpoint="previous.pt", **common)
    assert command == build_finetune_command(checkpoint="previous.pt", **common)
    assert not {"--config", "--fold-index", "--split-manifest", "--seed"} & set(command)
    assert backend.inference_command(checkpoint="previous.pt") == build_inference_command(
        checkpoint="previous.pt"
    )


def test_default_sampling_source_is_pinned_and_wrong_revision_is_rejected(tmp_path: Path) -> None:
    record = stage(tmp_path)
    assert record["source_revision"] == load_model_asset().revision
    assert verify(tmp_path) == record
    with pytest.raises(ValueError, match="must match"):
        validate_source_spec({"revision": "0" * 40})


@pytest.mark.parametrize(
    "name",
    [
        "rxnresid/train.py",
        "sar-runtime/sar_model_envs/engine.py",
        "sar-runtime/rxnresid.tar.gz",
    ],
)
def test_worker_rejects_source_or_environment_drift(tmp_path: Path, name: Any) -> None:
    stage(tmp_path)
    path = tmp_path / name
    path.write_bytes(path.read_bytes() + b"\n# unexpected drift\n")
    with pytest.raises(ValueError, match="changed"):
        verify(tmp_path)


def test_initializer_invokes_the_existing_verified_recipe(tmp_path: Path) -> None:
    record = stage(tmp_path)
    with patch("tricycle_sar.sampling_runtime.subprocess.run") as execute:
        initialize(tmp_path)
    argv = execute.call_args.args[0]
    assert argv[0] == sys.executable
    assert "--execute" in argv
    assert argv[argv.index("--source-sha256") + 1] == record["environment_files"]["rxnresid.tar.gz"]
    assert "sar_model_envs/engine.py" in argv[1]
    assert record["environment"]["environment_mode"] == "base-venv"


def test_staged_backend_and_feedback_import_without_workspace_or_site_packages(
    tmp_path: Path,
) -> None:
    stage(tmp_path)
    assert not (tmp_path / "tools").exists()
    paths = [str(tmp_path / name / "src") for name in PACKAGES]
    script = (
        f"import sys; sys.path[:0]={paths!r}; "
        "from tricycle_sar.sampling_runtime import verify; "
        f"verify(__import__('pathlib').Path({str(tmp_path)!r})); "
        "from tricycle_sampling.replay.mae import pre_update; "
        "from tricycle_sampling.replay.worker import fit, predict; "
        "from tricycle_sampling.replay.campaign import main; "
        "from tricycle_sampling.replay.suite import verified_plan; "
        "from tricycle_sampling.active_learning import load_experiment_spec; "
        "from tricycle_sampling.acquisition_worker import run_worker; "
        "from tricycle_sampling.space_snapshot import verify_frozen_space; "
        "assert 'tools' not in sys.modules and 'tricycle_exploration' not in sys.modules; "
        "sys.argv=['campaign', '--help']; main()"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
        cwd=tmp_path,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "prepare" in result.stdout


def test_frozen_vocabulary_applies_identically_to_train_refit_and_prediction(
    tmp_path: Path,
) -> None:
    prior = tmp_path / "prior.csv"
    prior.write_text("G_T_activate,prod_id\n0,8\n0,2\n")
    vocabulary = tmp_path / "routes.json"
    freeze_route_vocabulary(prior, vocabulary)
    assert json.loads(vocabulary.read_text())["route_id_vocabulary"] == {"2": 0, "8": 1}
    backend = get_backend("rxnresid")
    source = tmp_path / "source"
    options = {"source_directory": source, "route_vocabulary": vocabulary}
    for command in (
        backend.training_command(fold_index=0, **options),
        backend.refit_command(checkpoint="previous.pt", **options),
        backend.inference_command(checkpoint="previous.pt", **options),
    ):
        assert command[1:3] == ("-m", "tricycle_sar.rxnresid")
        assert command[command.index("--route-vocabulary") + 1] == str(vocabulary)
    assert route_command(("python", "predict.py")) == ("python", "predict.py")


def test_verification_rejects_staged_sar_api_drift(tmp_path: Path) -> None:
    stage(tmp_path)
    (tmp_path / "tricycle-sar/src/tricycle_sar/predictions.py").write_text("modified")
    with pytest.raises(ValueError, match="SAR API changed"):
        verify(tmp_path)


def test_sampling_delivery_only_contains_rxnresid_backend_assets(tmp_path: Path) -> None:
    record = stage(tmp_path)
    backend = get_backend("rxnresid")
    catalog = json.loads((tmp_path / "tricycle-sar/src/tricycle_sar/backends/catalog.json").read_text())
    assert set(catalog["models"]) == {"rxnresid"}
    assert record["method_id"] == backend.method["method_id"]
    assert not list(tmp_path.rglob("tokenizer.json"))
    for other in ("chemprop.tar.gz", "rxngraphormer.tar.gz", "t5chem.tar.gz"):
        assert not list(tmp_path.rglob(other))
