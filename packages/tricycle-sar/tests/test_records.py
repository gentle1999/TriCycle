from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tricycle_core.asset_store import resolve_input
from tricycle_core.toml import dump_toml, load_toml
from tricycle_core.training_protocol import (
    expand_training_config,
    file_sha256,
    protocol_file_source,
    resolve_training_registry,
)

from tricycle_sar.backends import CATALOG
from tricycle_sar.benchmark import BenchmarkRequest, make_splits, write_rows
from tricycle_sar.modelling_benchmark import check, prepare_modelling
from tricycle_sar.records import compact_tasks, publication_files, read_method_bindings
from tricycle_sar.results import (
    index_assets,
    prepare_training,
    read,
    verify,
)
from tricycle_sar.training_inputs import prepare_inputs

ROOT = Path(__file__).resolve().parents[3]
LOCK = ROOT / "configs/sar-training-protocols.lock.json"


def test_seed_and_fold_tasks_share_nested_hashes_once() -> None:
    hashes = [{"path": "dataset.csv", "sha256": "a" * 64}]
    rows = [
        {
            "name": f"seed-{seed}-fold-{fold}",
            "hashes": hashes,
            "model": "rxnresid",
            "seed": seed,
            "fold": fold,
        }
        for seed in (2, 3)
        for fold in range(5)
    ]
    defaults, specs = compact_tasks(rows)
    assert defaults == {"hashes": hashes, "model": "rxnresid"}
    assert len(specs) == 10
    assert [{**defaults, **row} for row in specs] == rows
    assert all(set(row) == {"name", "seed", "fold"} for row in specs)


@pytest.fixture
def experiment(tmp_path: Path) -> Any:
    rows = [
        {
            "path_id": f"p{index}",
            "rxn_smiles": "C>>C",
            "G_T_activate": index + 1,
            "reaction_free_energy": 0,
            "ene_id": index,
            "diene_id": 1,
        }
        for index in range(17)
    ]
    dataset = tmp_path / "dataset.csv"
    write_rows(dataset, rows)
    config = tmp_path / "experiment.toml"
    dump_toml(
        config,
        {
            "experiment_id": "compact",
            "models": ["fingerprint_rf"],
            "training_protocol": {"reference": str(LOCK)},
            "dataset": {
                "path": str(dataset),
                "sha256": file_sha256(dataset),
                "expected_rows": 17,
                "expected_groups": 17,
            },
        },
    )
    return config, dataset, rows


def test_new_run_uses_pinned_methods_and_one_record(experiment: Any, tmp_path: Path) -> None:
    config, _, _ = experiment
    run = prepare_modelling(config, run_dir=tmp_path / "run")
    assert (run / "input-assets.json").is_file()
    assert not any((run / name).exists() for name in ("dataset.csv", "splits.csv", "workspace"))
    assert not any(path.is_symlink() for path in run.rglob("*"))
    state = load_toml(run / "run.toml")
    assert state["models"] == ["fingerprint_rf"]
    assert state["methods"]["fingerprint_rf"].startswith("fingerprint_rf/train/")
    before = {p.relative_to(run) for p in run.rglob("*")}
    check(run)
    assert before == {p.relative_to(run) for p in run.rglob("*")}


def test_new_preparation_uses_package_backend_by_default(experiment: Any, tmp_path: Path) -> None:
    config, _, _ = experiment
    value = load_toml(config)
    value.pop("training_protocol")
    dump_toml(config, value)
    run = prepare_modelling(config, run_dir=tmp_path / "default-backend")
    state = load_toml(run / "run.toml")
    assert state["methods"]["fingerprint_rf"].startswith("fingerprint_rf/train/")
    assert not (run / "integrity.toml").exists()


def test_benchmark_template_prepares_explicit_data_and_existing_splits(
    experiment: Any, tmp_path: Path,
) -> None:
    _, dataset, rows = experiment
    value = load_toml(ROOT / "configs/example-sar-benchmark.toml")
    value["models"] = ["fingerprint_rf"]
    value["training_protocol"]["reference"] = str(LOCK)
    value["dataset"].update(path=str(dataset), sha256=file_sha256(dataset))
    config = tmp_path / "bound-benchmark.toml"
    request = BenchmarkRequest.from_mapping(
        expand_training_config(config, {**value, "split": {}}), tmp_path,
    )
    splits = tmp_path / "existing-splits.csv"
    write_rows(splits, make_splits(rows, request))
    value["split"].update(path=str(splits), sha256=file_sha256(splits))
    dump_toml(config, value)

    run = prepare_modelling(config, run_dir=tmp_path / "bound-run")
    check(run)
    assert (resolve_input(run, "dataset.csv")).read_bytes() == dataset.read_bytes()
    assert (resolve_input(run, "splits.csv")).read_bytes() == splits.read_bytes()
    assert load_toml(run / "run.toml")["models"] == ["fingerprint_rf"]


@pytest.mark.parametrize("section,unknown", [("dataset", "targets"), ("split", "paths")])
def test_locked_benchmark_rejects_unknown_nested_fields(
    experiment: Any, tmp_path: Path, section: str, unknown: str,
) -> None:
    config, _, _ = experiment
    value = load_toml(config)
    value.setdefault(section, {})[unknown] = "misspelled-field"
    dump_toml(config, value)
    run = tmp_path / "rejected"
    with pytest.raises(ValueError, match=f"unknown benchmark {section} fields: {unknown}"):
        prepare_modelling(config, run_dir=run)
    assert not run.exists()


@pytest.fixture
def published(result_package_factory: Any) -> Any:
    output = result_package_factory()
    return output, read_method_bindings(output / "METHOD_BINDINGS.csv")


def test_publisher_exports_one_prediction_table_and_rejects_runtime_evidence(
    published: Any,
) -> None:
    output, bindings = published
    model = "fingerprint_rf"
    result = verify(output)
    assert result["metrics"][model]["oof_mae"] == 1
    assert result["folds"] == 5
    assert result["oof_rows"] == 17
    assert {p.name for p in (output / "results").iterdir()} == {
        "oof-predictions.csv",
        "fold-metrics.csv",
        "model-summary.csv",
    }
    assert all(
        float(row["reported_y_true"]) != float(row["y_true"])
        for row in read(output / "results/oof-predictions.csv")
    )
    assert read_method_bindings(output / "METHOD_BINDINGS.csv") == bindings
    assert len((output / "METHOD_BINDINGS.csv").read_text().splitlines()) == 2
    assert not (output / "provenance").exists()
    assert not list(output.rglob("*.log"))
    assert not (output / "protocol-ref.json").exists()
    (output / "provenance").mkdir()
    (output / "provenance/worker.log").write_text("not publishable")
    with pytest.raises(ValueError, match="Runtime evidence"):
        publication_files(output)


@pytest.mark.parametrize("extra", ["raw/fingerprint_rf/fold-0.csv", "predictions.csv"])
def test_publication_rejects_duplicate_prediction_files(published: Any, extra: Any) -> None:
    output, _ = published
    path = output / "results" / extra
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("y_pred\n1\n")
    with pytest.raises(ValueError, match="Duplicate or unexpected result table"):
        publication_files(output)


def test_publication_rejects_duplicate_backend_reference(published: Any) -> None:
    output, _ = published
    (output / "protocol-ref.json").write_text("{}")
    with pytest.raises(ValueError, match="Runtime evidence"):
        publication_files(output)


@pytest.mark.parametrize("extra", ["scripts/verify.py", "reproduction/pyproject.toml"])
def test_new_publications_reject_copied_entrypoints_and_environments(
    published: Any, extra: Any
) -> None:
    output, _ = published
    path = output / extra
    path.parent.mkdir()
    path.write_text("copied implementation")
    with pytest.raises(ValueError, match="Runtime evidence"):
        publication_files(output)


def test_results_cli_verifies_without_optional_or_installed_packages(
    published: Any, tmp_path: Path
) -> None:
    output, _ = published
    before = {path: path.read_bytes() for path in output.rglob("*") if path.is_file()}
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        str(ROOT / "packages" / package / "src")
        for package in ("tricycle-core", "tricycle-bohr", "tricycle-sar")
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            "-m",
            "tricycle_sar",
            "results",
            "verify",
            "--result-dir",
            str(output),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert result["status"] == "verified"
    assert result["oof_rows"] == 17
    assert before == {path: path.read_bytes() for path in output.rglob("*") if path.is_file()}


def test_published_input_preparation_uses_current_method(
    tmp_path: Path, result_package_factory: Any
) -> None:
    model, fold = "chemprop_cgr", 1
    archive = result_package_factory(model, recorded=True)
    expected = read_method_bindings(archive / "METHOD_BINDINGS.csv")[model, fold]
    _, registry = resolve_training_registry(CATALOG)
    recorded = expected
    expected = registry["models"][model]["default_method"]
    assert recorded != expected
    result = prepare_training(archive, model, fold, tmp_path / "inputs")
    assert result["method_id"] == expected
    output = Path(result["run_dir"])
    task = load_toml(resolve_input(output, result["task"]))
    assert task["fold"] == fold
    assert file_sha256(
        resolve_input(output, "common") / task["bundle_name"] / "data/splits/ground-truth-5fold/assignments.csv"
    ) == file_sha256(archive / "data/splits.csv")


def test_asset_index_reuses_fixed_wheel_and_rejects_corrupted_cache(
    tmp_path: Path, result_package_factory: Any
) -> None:
    archive = result_package_factory("molclr")
    manifest, registry = resolve_training_registry(CATALOG)
    method = registry["methods"][registry["models"]["molclr"]["default_method"]]
    entry = next(e for e in method["files"] if e["path"].endswith(".whl"))
    source = protocol_file_source(manifest, entry)
    output = tmp_path / "assets"
    result = index_assets(archive, [source, source], output)
    assert result == {"indexed": [entry["sha256"]], "network": False}
    target = output / entry["sha256"]
    original = target.stat().st_mtime_ns
    index_assets(archive, [source], output)
    assert target.stat().st_mtime_ns == original
    target.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="corrupted"):
        index_assets(archive, [source], output)


def test_unrecognized_asset_is_rejected_before_index_creation(
    tmp_path: Path, published: Any
) -> None:
    archive, _ = published
    source = tmp_path / "unknown.bin"
    source.write_bytes(b"unknown asset")
    output = tmp_path / "assets"
    with pytest.raises(ValueError, match="Unrecognized fixed asset"):
        index_assets(archive, [source], output)
    assert not output.exists()


@pytest.mark.parametrize("command", ["prepare", "assets"])
def test_reproduction_cannot_write_inside_the_result_package(published: Any, command: Any) -> None:
    output, _ = published
    with pytest.raises(ValueError, match="outside the published result package"):
        if command == "prepare":
            prepare_training(output, "fingerprint_rf", 0, output / "inputs")
        else:
            index_assets(output, [], output / "assets")
    assert verify(output)["status"] == "verified"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"model": "unknown"}, "unknown identities"),
        ({"path_id": "unknown"}, "unknown identities"),
        ({"fold": "99"}, "unknown identities"),
        ({"row_index": "99"}, "OOF row index"),
        ({"y_true": "-99"}, "canonical GT"),
        ({"reported_y_true": "-99"}, "native GT"),
        ({"y_pred": "nan"}, "finite prediction"),
        ({"absolute_error": "99"}, "absolute error"),
    ],
)
def test_single_prediction_table_validates_identity_labels_and_values(
    published: Any, changes: Any, message: Any
) -> None:
    output, _ = published
    path = output / "results/oof-predictions.csv"
    rows = read(path)
    rows[0].update(changes)
    write_rows(path, rows)
    with pytest.raises(ValueError, match=message):
        verify(output, integrity=False)


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "wrong_fold"])
def test_single_prediction_table_requires_each_test_path_exactly_once(
    published: Any, mutation: Any
) -> None:
    output, _ = published
    path = output / "results/oof-predictions.csv"
    rows = read(path)
    if mutation == "duplicate":
        rows.append(rows[0])
    elif mutation == "missing":
        rows.pop()
    else:
        rows[0]["fold"] = str((int(rows[0]["fold"]) + 1) % 5)
    write_rows(path, rows)
    with pytest.raises(ValueError, match="OOF"):
        verify(output, integrity=False)


def test_training_inputs_use_method_and_original_split_without_upload_extras(
    tmp_path: Path, published: Any,
) -> None:
    archive, _ = published
    result = prepare_inputs(
        archive / "data/dataset.csv",
        archive / "data/splits.csv",
        CATALOG,
        "rxnresid",
        0,
        tmp_path / "input",
    )
    output = Path(result["run_dir"])
    assert file_sha256(
        resolve_input(output, "common/training_bundle/data/splits/ground-truth-5fold/assignments.csv")
    ) == file_sha256(archive / "data/splits.csv")
    assert not list(output.glob("common/sar_model_envs/init_*.sh"))
    assert not (output / "provenance").exists()
    assert load_toml(resolve_input(output, result["task"]))["fold"] == 0
    manifest = json.loads((resolve_input(output, "common/training_bundle/training-bundle.json")).read_text())
    assert manifest["dataset_sha256"] == file_sha256(archive / "data/dataset.csv")


def test_missing_fixed_assets_are_rejected_before_output_creation(
    tmp_path: Path, published: Any
) -> None:
    archive, _ = published
    output = tmp_path / "not-created"
    with pytest.raises(ValueError, match="Required fixed assets missing"):
        prepare_inputs(
            archive / "data/dataset.csv",
            archive / "data/splits.csv",
            CATALOG,
            "reactiont5",
            2,
            output,
        )
    assert not output.exists()


def test_published_results_verify_without_loading_executable_backends(
    monkeypatch: pytest.MonkeyPatch, result_package_factory: Any,
) -> None:
    archive = result_package_factory(recorded=True)
    def reject(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Scientific result verification must not resolve executable code")

    monkeypatch.setattr("tricycle_core.training_protocol.resolve_training_registry", reject)
    result = verify(archive)
    assert result["verification_scope"] == "recorded-scientific-results"
    assert result["folds"] == 5
    assert result["oof_rows"] == 17
