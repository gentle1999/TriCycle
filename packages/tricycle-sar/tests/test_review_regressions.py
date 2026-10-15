from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from tricycle_bohrsubmit import InMemoryBohrGateway
from tricycle_core import Artifact, ExecutionContext, ModelMode
from tricycle_core.asset_store import resolve_input
from tricycle_core.errors import InvalidRequestError
from tricycle_core.io import file_sha256
from tricycle_core.toml import dump_toml, load_toml
from tricycle_core.training_protocol import protocol_file_source, resolve_training_registry

from tricycle_sar import (
    RxnResidJobRequest,
    SarPlugin,
    build_training_command,
    get_backend,
    list_backends,
)
from tricycle_sar.backends import CATALOG
from tricycle_sar.benchmark import BenchmarkRequest, make_splits, write_rows
from tricycle_sar.modelling_benchmark import prepare_modelling, validate_models
from tricycle_sar.predictions import validate_rows
from tricycle_sar.results import publish, verify

Experiment = tuple[Path, Path, list[dict[str, str]], list[dict[str, Any]]]
PublicationInputs = tuple[Path, Path, dict, dict]


@pytest.fixture
def experiment(tmp_path: Path) -> Experiment:
    rows = [
        {
            "path_id": f"path-{i}",
            "rxn_smiles": "C>>C",
            "G_T_activate": str(i + 1),
            "reaction_free_energy": "0",
            "ene_id": f"ene-{i}",
            "diene_id": "diene-a",
        }
        for i in range(17)
    ]
    dataset, splits = tmp_path / "dataset.csv", tmp_path / "splits.csv"
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
    assignments = make_splits(rows, request)
    write_rows(splits, assignments)
    return dataset, splits, rows, assignments


def prepared_run(tmp_path: Path, experiment: Experiment, models: list[str]) -> Path:
    dataset, _, _, _ = experiment
    config = tmp_path / "experiment.toml"
    dump_toml(
        config,
        {"models": models, "dataset": {"path": str(dataset), "sha256": file_sha256(dataset)}},
    )
    return prepare_modelling(config, run_dir=tmp_path / "run")


def publication_inputs(experiment: Experiment) -> PublicationInputs:
    dataset, splits, rows, assignments = experiment
    model = "fingerprint_rf"
    predictions = {
        (model, fold): [
            {
                "reaction_id": row["path_id"],
                "row_index": i,
                "target_name": "G_T_activate",
                "y_true": row["G_T_activate"],
                "y_pred": float(row["G_T_activate"]) + 1,
            }
            for i, row in enumerate(rows)
            if any(
                a["row_id"] == row["path_id"] and int(a["fold"]) == fold and a["split"] == "test"
                for a in assignments
            )
        ]
        for fold in range(5)
    }
    bindings = {(model, fold): get_backend(model).method["method_id"] for fold in range(5)}
    return dataset, splits, predictions, bindings


def test_current_catalog_models_require_canonical_unique_ids() -> None:
    assert validate_models(list(list_backends())) == list(list_backends())
    for names in ([], ["chemprop"], ["chemprop", "chemprop_cgr"], ["fingerprint_rf", "fingerprint_rf"], ["reamvp"]):
        with pytest.raises(ValueError, match="unique subset"):
            validate_models(names)


def test_all_train_manifest_binds_current_upstream_record_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tricycle_sar.rxnresid import write_all_train_manifest

    # Exercise the delivered split implementation without loading the Torch dataset.
    rows = [
        SimpleNamespace(path_id='a', group_id='g', raw_rxn_smiles='C>>C', activation_energy=1.0),
        SimpleNamespace(path_id='b', group_id='g', raw_rxn_smiles='C>>C', activation_energy=2.0),
    ]

    class Dataset:
        def __init__(self, path: Path, **kwargs: Any) -> None:
            self.rows = rows
            self.group_ids = ['g']
            self.group_index = {'g': 0}

        def __len__(self) -> int:
            return len(self.group_ids)

    dataset_module = ModuleType('rxnresid.data.dataset')
    dataset_module.ReactionGroupDataset = Dataset  # type: ignore[attr-defined]
    dataset_module.ReactionPathDataset = Dataset  # type: ignore[attr-defined]
    split_module: Any = ModuleType('rxnresid.data.split')
    monkeypatch.setitem(sys.modules, dataset_module.__name__, dataset_module)
    monkeypatch.setitem(sys.modules, split_module.__name__, split_module)
    with tarfile.open(CATALOG.parent / 'sources/rxnresid.tar.gz') as archive:
        source = archive.getmember('src/rxnresid/data/split.py')
        handle = archive.extractfile(source)
        assert handle is not None
        exec(compile(handle.read(), source.name, 'exec'), split_module.__dict__)

    output = tmp_path / 'split.json'
    assert write_all_train_manifest(tmp_path / 'training.csv', output) == 2
    saved = json.loads(output.read_text())
    assert saved['dataset_row_count'] == 2
    manifest = split_module.SplitManifest.from_mapping(saved)
    dataset = Dataset(tmp_path / 'training.csv')
    split = manifest.resolve(dataset, 0)
    assert split.train == [0] and not split.valid and not split.test
    rows[0].activation_energy = 9.0
    with pytest.raises(ValueError, match='record fingerprint'):
        manifest.resolve(dataset, 0)


def test_all_preparation_families_read_caller_group_config_outside_checkout(
    tmp_path: Path, experiment: Experiment, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tricycle_sar.preparation.all_models import prepare

    run = prepared_run(tmp_path, experiment, ["fingerprint_rf", "uam", "chemprop_cgr", "rxnresid"])
    group = tmp_path / "group.toml"
    dump_toml(group, {"project_id": "caller-project", "job_group_id": "caller-group"})
    monkeypatch.chdir(tmp_path)
    output = prepare(
        run, tmp_path / "requests", group_name="caller-experiment",
        group_config=group,
    )
    order = load_toml(output / "submit-order.toml")
    assert {entry["name"] for entry in order["models"]} == {
        "classical", "uam", "chemprop_cgr", "rxnresid",
    }
    for entry in order["models"]:
        request = load_toml(Path(entry["request"]))
        assert request["project_id"] == "caller-project"
        assert request["job_group_name"] == "caller-experiment"
        assert request["auto_job_group"]
        assert request["job_group_id"] == ""


def test_request_without_scheduling_config_fails_before_writing(tmp_path: Path) -> None:
    from tricycle_sar.submission import write_bohr_request

    output = tmp_path / "requests"
    with pytest.raises(ValueError, match="explicit group_config or project_id"):
        write_bohr_request(
            "fingerprint_rf", output, tmp_path / "bundle",
            task_defaults={}, task_specs=[], workers=1, job_prefix="test",
            max_run_time="1", filename="bohr.toml",
        )
    assert not output.exists()


def test_rf_subset_prepares_five_tasks_without_deep_model_assets(
    tmp_path: Path, experiment: Experiment
) -> None:
    from tricycle_sar.preparation.all_models import prepare

    run = prepared_run(tmp_path, experiment, ["fingerprint_rf"])
    group = tmp_path / "group.toml"
    dump_toml(group, {"project_id": "test-project", "job_group_id": "test-group"})
    output = prepare(run, tmp_path / "prepared", group_name="review-regression", group_config=group)
    order = load_toml(output / "submit-order.toml")
    assert order["bohr_tasks"] == 5
    assert order["internal_tasks"] == 0
    assert [m["name"] for m in order["models"]] == ["classical"]
    request = load_toml(Path(order["models"][0]["request"]))
    assert len(request["task_specs"]) == 5
    assert request["task_defaults"]["models"] == ["fingerprint_rf"]
    assert not (run / "workspace/configs/tricycle-reactiont5.json").exists()


def test_uam_subset_uses_registered_template_without_molclr_checkpoint(
    tmp_path: Path, experiment: Experiment
) -> None:
    from tricycle_sar.preparation.all_models import prepare

    run = prepared_run(tmp_path, experiment, ["uam"])
    group = tmp_path / "group.toml"
    dump_toml(group, {"project_id": "test-project", "job_group_id": "test-group"})
    output = prepare(run, tmp_path / "prepared", group_name="review-regression", group_config=group)
    order = load_toml(output / "submit-order.toml")
    assert [m["name"] for m in order["models"]] == ["uam"]
    bundle = resolve_input(output / "model-bundles/molecular/uam", "molecular_bundle")
    config = load_toml(bundle / "configs/tricycle-uam.toml")
    assert config["dataset_sha256"] == file_sha256(bundle / config["dataset"])
    assert not (bundle / "assets/molclr").exists()


def test_cumulative_main_and_auxiliary_fits_use_all_train_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tricycle_sar import rxnresid

    def write_split(data: Path, path: Path, **kwargs: Any) -> None:
        path.write_text(
            json.dumps(
                {
                    "strategy": "active_learning_all_selected_train",
                    "folds": [
                        {
                            "fold_index": 0,
                            "train_group_ids": ["a", "b"],
                            "valid_group_ids": [],
                            "test_group_ids": [],
                        }
                    ],
                }
            )
        )

    def write_config(path: Path, base: dict, **kwargs: Any) -> None:
        path.write_text(f'data:\n  split_manifest: {json.dumps(str(kwargs["split_manifest"]))}\n')

    def execute(command: tuple[str, ...], **kwargs: Any) -> None:
        output = Path(command[command.index("--output") + 1])
        output.mkdir(parents=True)
        (output / "checkpoint.pt").write_bytes(b"checkpoint")

    monkeypatch.setattr(rxnresid, "write_all_train_manifest", write_split)
    monkeypatch.setattr(rxnresid, "write_round_config", write_config)
    monkeypatch.setattr(rxnresid, "load_base_config", lambda source: {})
    with patch("subprocess.run", side_effect=execute) as run:
        for role in ("main", "auxiliary"):
            checkpoint = rxnresid.fit_all_revealed(
                tmp_path / "labels.csv",
                tmp_path / role,
                {"epochs": 2, "batch_size_paths": 4, "device": "cpu"},
                source=tmp_path / "source",
            )
            assert checkpoint.is_file()
            command = run.call_args.args[0]
            assert "--train-on-train-valid-from-scratch" in command
            assert "--selection-refit-from-scratch" not in command


def test_duplicate_paths_with_correct_pair_counts_are_rejected() -> None:
    a = {
        "path_id": "path-a",
        "ene_id": "a",
        "diene_id": "b",
        "prediction": "2",
        "target": "3",
        "predictive_variance": "1",
        "epistemic_variance": "1",
        "aleatoric_variance": "0",
    }
    b = {**a, "path_id": "path-b"}
    validate_rows([a, b], expected=[a, b])
    with pytest.raises(ValueError, match="duplicate"):
        validate_rows([a, a], expected=[a, b])
    with pytest.raises(ValueError, match="coverage"):
        validate_rows([a, {**b, "path_id": "unknown"}], expected=[a, b])


def test_single_job_runner_uses_the_same_cumulative_training_mode(tmp_path: Path) -> None:
    from tricycle_sar.single_job import input_hashes, run

    inputs = tmp_path / "job-inputs"
    inputs.mkdir()
    (inputs / "config.yaml").write_text("data:\n  split_manifest: split.json\n")
    (inputs / "split.json").write_text(
        json.dumps(
            {
                "strategy": "active_learning_all_selected_train",
                "folds": [
                    {
                        "fold_index": 0,
                        "train_group_ids": ["group-a"],
                        "valid_group_ids": [],
                        "test_group_ids": [],
                    }
                ],
            }
        )
    )
    task = tmp_path / "task.toml"
    dump_toml(
        task,
        {
            "mode": "train",
            "backend": {"model": "rxnresid"},
            "input_hashes": input_hashes(inputs),
            "command": list(
                build_training_command(config="config.yaml", fold_index=0, output="outputs/train")
            ),
        },
    )
    with (
        patch("tricycle_sar.single_job.verify"),
        patch("tricycle_sar.single_job.subprocess.run") as execute,
    ):
        run(task)
    command = execute.call_args.args[0]
    assert "--train-on-train-valid-from-scratch" in command
    assert command[0] == sys.executable
    assert command[1] == str(tmp_path / "rxnresid/train.py")
    assert execute.call_args.kwargs["cwd"] == inputs


def test_acquisition_worker_rejects_missing_runtime_before_any_prediction(tmp_path: Path) -> None:
    from tricycle_sampling.acquisition_worker import SCHEMA, WorkerInputError, run_worker

    (tmp_path / "rxnresid").mkdir()
    task = tmp_path / "request.toml"
    dump_toml(
        task,
        {
            "schema": SCHEMA,
            "model_snapshot_id": "snapshot",
            "source_revision": "a615f4161cb612f5ad98136d4965685009c62bab",
            "source_directory": "rxnresid",
        },
    )
    with (
        patch("tricycle_sampling.acquisition_worker.subprocess.run") as execute,
        pytest.raises(WorkerInputError, match="runtime validation failed"),
    ):
        run_worker(task)
    execute.assert_not_called()


def test_legacy_runner_checks_expected_path_identities(tmp_path: Path) -> None:
    from tricycle_sampling.trajectory_worker import _parse_prediction_output

    row = {
        "path_id": "one",
        "ene_id": "ene-a",
        "diene_id": "diene-a",
        "prediction": "2",
        "target": "3",
        "predictive_variance": "1",
        "epistemic_variance": "1",
        "aleatoric_variance": "0",
    }
    file = tmp_path / "predictions.csv"
    write_rows(file, [row, row])
    with pytest.raises(ValueError, match="duplicate"):
        _parse_prediction_output(
            file,
            expected_rows=2,
            expected_pairs={"ene-a+diene-a"},
            expected=[row, {**row, "path_id": "two"}],
        )


def test_string_ids_publish_and_verify_through_a_symlink(
    tmp_path: Path, experiment: Experiment
) -> None:
    parent = tmp_path / "real"
    parent.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(parent, target_is_directory=True)
    output = publish(
        *publication_inputs(experiment),
        CATALOG,
        alias / "published",
        expected_protocol_sha256=file_sha256(CATALOG),
    )
    assert output == parent / "published"
    assert verify(alias / "published")["rows_per_model"] == 17


def test_failed_publication_does_not_leave_partial_results(
    tmp_path: Path, experiment: Experiment
) -> None:
    output = tmp_path / "published"
    with (
        patch("tricycle_sar.results.verify", side_effect=ValueError("audit failed")),
        pytest.raises(ValueError, match="audit failed"),
    ):
        publish(
            *publication_inputs(experiment),
            CATALOG,
            output,
            expected_protocol_sha256=file_sha256(CATALOG),
        )
    assert not output.exists()
    assert not list(tmp_path.glob(".sar-publication-*"))


@pytest.mark.parametrize('shape', ['old-field', 'both-fields', 'missing-backend', 'invalid-backend'])
def test_result_verification_requires_the_current_backend_hash_field(
    tmp_path: Path, experiment: Experiment, shape: str,
) -> None:
    output = publish(
        *publication_inputs(experiment), CATALOG, tmp_path / 'published',
        expected_protocol_sha256=file_sha256(CATALOG),
    )
    assert verify(output)['rows_per_model'] == 17
    path = output / 'manifest.json'
    manifest = json.loads(path.read_text())
    if shape in {'old-field', 'both-fields'}:
        manifest['training_protocol_manifest_sha256'] = manifest['backend']['sha256']
    if shape in {'old-field', 'missing-backend'}:
        del manifest['backend']
    elif shape == 'invalid-backend':
        manifest['backend'] = []
    path.write_text(json.dumps(manifest))
    saved = path.read_bytes()
    with pytest.raises(ValueError, match='backend'):
        verify(output, integrity=False)
    assert path.read_bytes() == saved


def test_installed_layout_resolves_shared_core_without_workspace_paths(tmp_path: Path) -> None:
    import importlib.util

    site = tmp_path / "site-packages"
    for module in ("tricycle_core", "tricycle_bohr", "tricycle_bohrsubmit", "tricycle_sar"):
        spec = importlib.util.find_spec(module)
        assert spec and spec.origin
        shutil.copytree(
            Path(spec.origin).parent,
            site / module,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    code = f"import sys; sys.path.insert(0, {str(site)!r}); from tricycle_sar import get_backend, list_backends; assert len(list_backends()) == 11; print(get_backend('rxnresid').method['method_id'])"
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == get_backend("rxnresid").method["method_id"]


def test_cpu_environment_initialization_uses_cpu_stage_and_hides_cuda(tmp_path: Path) -> None:
    from tricycle_sar.backends.execution import execute

    common, result = tmp_path / "common", tmp_path / "results"
    (common / "environment_bundle").mkdir(parents=True)
    task = tmp_path / "task.toml"
    dump_toml(
        task,
        {
            "model": "fingerprint_rf",
            "environment_model": "classical",
            "fold": 0,
            "bundle_name": "environment_bundle",
        },
    )
    directory = tmp_path / ".sar-model-environment/classical"
    directory.mkdir(parents=True)
    (directory / "runtime.json").write_text("{}")
    dump_toml(directory / "state.toml", {"model": "classical", "status": "environment-installed"})
    with patch(
        "tricycle_sar.backends.execution.subprocess.run",
        return_value=SimpleNamespace(stdout=json.dumps({"directory": str(directory)})),
    ) as runner:
        execute(task, common, result, initialize_only=True)
    command = runner.call_args_list[0].args[0]
    assert command[command.index("--accelerator") + 1] == "cpu"
    assert runner.call_args_list[0].kwargs["env"]["CUDA_VISIBLE_DEVICES"] == ""


@pytest.fixture
def native_request(tmp_path: Path) -> RxnResidJobRequest:
    source = tmp_path / "RxnResid"
    source.mkdir()
    entry = next(
        e for e in get_backend("rxnresid").method["files"] if e["path"] == "common/rxnresid.tar.gz"
    )
    with tarfile.open(protocol_file_source(CATALOG, entry)) as archive:
        archive.extractall(source, filter="data")
    dataset = source / "inputs/custom.csv"
    dataset.parent.mkdir()
    dataset.write_text("path_id,target\na,2\n")
    env = get_backend("rxnresid").environment
    return RxnResidJobRequest(
        model_name="rxnresid",
        mode=ModelMode.TRAIN,
        image=env["image"],
        machine_type=env["machine_type"],
        dataset=Artifact("custom", str(dataset)),
        code_artifact=Artifact("source", str(source), kind="directory"),
        input_directory=str(source),
        command=build_training_command(fold_index=0),
    )


def test_sar_planning_and_submission_validate_real_source_and_input_drift(
    tmp_path: Path, native_request: RxnResidJobRequest
) -> None:
    plugin = SarPlugin()
    context = ExecutionContext("review-regression", workdir=tmp_path)
    plan = plugin.plan_job(native_request, context)
    record = plan.parameters["bohr_job"]["parameters"]["sar_backend"]
    assert record["backend"]["method_id"] == get_backend("rxnresid").method["method_id"]
    assert native_request.dataset is not None
    assert native_request.code_artifact is not None
    Path(native_request.dataset.location).write_text("changed")
    gateway = InMemoryBohrGateway()
    with pytest.raises(InvalidRequestError, match="inputs changed"):
        plugin.submit_to_bohr(
            plan, gateway, ExecutionContext("review-live", workdir=tmp_path, dry_run=False)
        )
    assert not gateway.jobs
    Path(native_request.code_artifact.location, "train.py").write_text("wrong source")
    with pytest.raises(InvalidRequestError, match="source differs"):
        plugin.plan_job(native_request, context)


def test_sar_rejects_an_arbitrary_image_or_launcher(native_request: RxnResidJobRequest) -> None:
    from dataclasses import replace

    for changes in ({"image": "unverified/image:latest"}, {"command": ("bash", "run.sh")}):
        with pytest.raises(InvalidRequestError):
            SarPlugin().plan_job(replace(native_request, **changes), ExecutionContext("invalid"))


def test_sar_submission_stages_the_shared_environment_and_verified_api(
    tmp_path: Path, native_request: RxnResidJobRequest
) -> None:
    from tricycle_sar.sampling_runtime import verify as verify_runtime

    plugin = SarPlugin()
    plan = plugin.plan_job(native_request, ExecutionContext("plan", workdir=tmp_path))
    gateway = InMemoryBohrGateway()
    result = plugin.submit_to_bohr(
        plan, gateway, ExecutionContext("submit", workdir=tmp_path, dry_run=False)
    )
    assert result.external_id is not None
    workers = list((tmp_path / "runs").glob("*/*/submit"))
    assert len(workers) == 1
    from tricycle_sar.delivery import materialize

    execution = tmp_path / "execution"
    materialize(workers[0], execution)
    assert not (workers[0] / "job-inputs").exists()
    record = verify_runtime(execution)
    assert record == plan.parameters["bohr_job"]["parameters"]["sar_backend"]["backend"]
    task = load_toml(execution / "task.toml")
    assert task["command"] == list(native_request.command)
    assert native_request.dataset is not None
    assert (execution / "job-inputs/inputs/custom.csv").read_bytes() == Path(
        native_request.dataset.location
    ).read_bytes()
    assert "tricycle_sar.single_job" in (execution / "worker.sh").read_text()


def test_current_catalog_validates_the_shared_runtime() -> None:
    reference = CATALOG
    _, registry = resolve_training_registry(reference)
    assert len(registry["models"]) == 11
    method = registry["methods"][registry["models"]["rxnresid"]["default_method"]]
    engine = next(
        entry for entry in method["files"] if entry["path"] == "common/sar_model_envs/engine.py"
    )
    assert engine["sha256"] == file_sha256(CATALOG.parent / "environments/engine.py")


def test_single_job_returns_only_generated_training_results(tmp_path: Path) -> None:
    from tricycle_sar.single_job import input_hashes, run

    inputs = tmp_path / "job-inputs"
    inputs.mkdir()
    (inputs / "previous.pt").write_bytes(b"uploaded")
    task = tmp_path / "task.toml"
    dump_toml(task, {
        "mode": "finetune", "backend": {}, "input_hashes": input_hashes(inputs),
        "command": ["python", "train.py", "--refit", "--checkpoint", "previous.pt",
                    "--output", "outputs/refit"],
    })

    def train(command: list[str], **kwargs: Any) -> None:
        work = Path(command[command.index("--output") + 1])
        assert work == tmp_path / "job-work/training"
        work.mkdir(parents=True)
        for name in ("checkpoint.pt", "refit_summary.json", "refit-input.pt", "preprocessed.csv"):
            (work / name).write_text(name)
        raise subprocess.CalledProcessError(7, command)

    with patch("tricycle_sar.single_job.verify"), patch(
        "tricycle_sar.single_job.subprocess.run", side_effect=train
    ), pytest.raises(subprocess.CalledProcessError):
        run(task)
    assert {p.name for p in (tmp_path / "outputs/refit").iterdir()} == {
        "checkpoint.pt", "refit_summary.json"
    }
    assert (inputs / "previous.pt").read_bytes() == b"uploaded"
