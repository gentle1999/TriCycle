from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from tricycle_core.io import file_sha256 as digest
from tricycle_core.toml import dump_toml
from tricycle_sampling import LITERATURE_BASELINE_STRATEGIES
from tricycle_sampling import trajectory_worker as runner
from tricycle_sampling.experiment import EXPERIMENT_SAMPLING_SCHEMA, reactant_structure_features
from tricycle_sampling.replay import baselines as replay_baselines


@pytest.mark.parametrize("schema", [None, "unsupported-schema"])
def test_replay_refuses_missing_or_unsupported_schema_before_reading_evidence(
    tmp_path: Path, schema: str | None,
) -> None:
    with pytest.raises(ValueError, match="current sampling_experiment schema"):
        replay_baselines.make(
            tmp_path / "absent-decision", {"sampling_experiment": schema},
        )
    assert not list(tmp_path.iterdir())


def test_replay_does_not_reinterpret_an_unsupported_strategy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="registered literature strategy"):
        replay_baselines.make(tmp_path, {
            "sampling_experiment": EXPERIMENT_SAMPLING_SCHEMA,
            "sampling_strategy": "random",
        })
    assert not list(tmp_path.iterdir())


def test_replay_preparation_does_not_replace_an_explicit_unknown_schema(tmp_path: Path) -> None:
    from tricycle_sampling.replay.campaign import prepare
    from tricycle_sampling.replay.suite import prepare_suite

    request = tmp_path / "request.toml"
    dump_toml(request, {
        "sampling_experiment": "unsupported-schema", "sampling_strategy": "predictive-uncertainty",
    })
    for operation in (prepare, prepare_suite):
        with pytest.raises(ValueError, match="Unsupported sampling_experiment schema"):
            operation(request, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_both_controllers_produce_identical_selections_and_common_input_hash(
    tmp_path: Path,
) -> None:
    rows = [
        {"pair_id": f"e{i}+d", "ene_smiles": ene, "diene_smiles": "C=CC=C"}
        for i, ene in enumerate(("C=C", "CC=C", "CCC=C"))
    ]
    reference = {"pair_id": "selected", "ene_smiles": "C=C", "diene_smiles": "C=CC=C"}
    scores = {
        row["pair_id"]: {
            "prediction_mean": 20.0 + i,
            "predictive_variance": 5.0 - i,
            "epistemic_variance": 4.0 - i,
            "product_count": 2.0,
        }
        for i, row in enumerate(rows)
    }
    directory = tmp_path / "decision"
    directory.mkdir()

    def save(name: str, value: Any) -> None:
        (directory / name).write_text(json.dumps(value))

    save("task.json", {"budget": 2, "sampled_count": 1})
    save(
        "pair-candidates.json",
        {
            "candidates": [
                {
                    "candidate_id": r["pair_id"],
                    "payload": {
                        **scores[r["pair_id"]],
                        "design_features": list(
                            reactant_structure_features(r["ene_smiles"] + "." + r["diene_smiles"])
                        ),
                    },
                }
                for r in rows
            ]
        },
    )
    save("reference-features.json", {"selected": list(reactant_structure_features("C=C.C=CC=C"))})
    save(
        "evidence-hashes.json",
        {
            name: digest(directory / name)
            for name in ("task.json", "pair-candidates.json", "reference-features.json")
        },
    )
    (tmp_path / "status.tsv").write_text("checked")
    (tmp_path / "download-results.tsv").write_text("checked")
    for strategy in LITERATURE_BASELINE_STRATEGIES:
        for name in ("response.json", "baseline-ranking.json"):
            (directory / name).unlink(missing_ok=True)
        decision = tmp_path / "runner-decision.json"
        selected, method = runner._select(
            strategy,
            rows,
            budget=2,
            seed=42,
            scores=scores,
            reference_rows=[reference],
            matched=True,
            design_size=3,
            offset=0,
            decision_path=decision,
        )
        config = {
            "sampling_strategy": strategy,
            "sampling_experiment": EXPERIMENT_SAMPLING_SCHEMA,
            "baseline_policy_sha256": digest(Path(replay_baselines.__file__)),
            "fixed_seed": 42,
            "round_count": 4,
            "batch_size": 1,
        }
        with patch.object(replay_baselines, "validate_calculation_evidence"):
            result = replay_baselines.make(directory, config)
            assert result == replay_baselines.make(directory, config)
        assert method == strategy
        assert [row["pair_id"] for row in selected] == result["chosen"]
        recorded = json.loads(decision.read_text())
        assert (
            recorded["metadata"]["sampling_experiment"]
            == result["definition"]["sampling_experiment"]
        )
        assert (
            recorded["metadata"]["sampling_protocol"]["doi"]
            == result["definition"]["sampling_protocol"]["doi"]
        )


def test_source_snapshot_does_not_share_workspace_inode(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    target = tmp_path / "frozen.py"
    source.write_text("original\n")
    from tricycle_core.io import copy_snapshot_file

    copy_snapshot_file(source, target)
    source.write_text("changed\n")
    assert target.read_text() == "original\n"
    assert source.stat().st_ino != target.stat().st_ino


def test_replay_train_refit_and_predict_use_sar_backend(tmp_path: Path) -> None:
    from unittest.mock import Mock

    from tricycle_sampling.acquisition_worker import _prediction_command
    from tricycle_sampling.replay import worker as worker
    from tricycle_sar import get_backend

    backend = Mock(wraps=get_backend("rxnresid"))
    recipe = {"epochs": 300, "batch_size_paths": 512, "device": "cpu"}
    data = tmp_path / "training.csv"
    fields: list[str] = list(runner.NATIVE_COLUMNS)
    rows = [
        {
            "path_id": "path",
            "rxn_smiles": "C>>C",
            "G_T_activate": "2",
            "ene_id": "1",
            "diene_id": "2",
            "prod_id": "0",
            "source_mapped_reaction_id": "r",
            "source_row_index": "0",
        }
    ]
    runner._write_csv(data, fields, rows)
    commands = []

    def execute(command: list[str], **options: Any) -> None:
        commands.append(command)
        output = Path(command[command.index("--output") + 1])
        if "--data" in command:
            predicted = [
                {
                    **rows[0],
                    "prediction": "3",
                    "target": "2",
                    "epistemic_variance": "1",
                    "aleatoric_variance": "1",
                    "predictive_variance": "2",
                }
            ]
            runner._write_csv(output, list(predicted[0]), predicted)
        else:
            output.mkdir()
            (output / "checkpoint.pt").write_bytes(b"weights")

    from tricycle_sar import backends as backend_module
    from tricycle_sar import rxnresid as shared

    with (
        patch.object(runner, "ROOT", tmp_path),
        patch.object(worker, "get_backend", return_value=backend),
        patch.object(backend_module, "get_backend", return_value=backend),
        patch.object(shared, "load_base_config", return_value={}),
        patch.object(shared, "write_all_train_manifest"),
        patch.object(shared, "write_round_config"),
        patch.object(shared, "prepare_refit_checkpoint"),
        patch.object(runner, "_run_logged", side_effect=execute),
        patch("subprocess.run", side_effect=execute),
    ):
        initial = worker.fit(data, tmp_path / "initial", recipe, None)
        refit = worker.fit(data, tmp_path / "refit", recipe, initial)
        predictions = worker.predict(refit, data, tmp_path / "predictions.csv", recipe)
    assert predictions[0]["prediction"] == "3"
    backend.training_command.assert_called_once()
    backend.refit_command.assert_called_once()
    backend.inference_command.assert_called_once()
    assert commands[0][commands[0].index("--seed") + 1] == "42"
    assert not {"--config", "--fold-index", "--split-manifest", "--seed"} & set(commands[1])
    assert commands[2] == _prediction_command(
        python=commands[2][0],
        script=tmp_path / "rxnresid/predict.py",
        checkpoint=refit,
        data=data,
        output=tmp_path / "predictions.csv",
        batch_size=512,
        cpu=True,
        mixed_precision=None,
        component_id_columns=("ene_id", "diene_id"),
    )


def test_matched_suite_freezes_all_non_strategy_inputs(tmp_path: Path) -> None:
    from tricycle_core.toml import dump_toml
    from tricycle_sampling.replay import suite as suite

    config = {
        "suite_id": "matched",
        "sampling_strategies": list(LITERATURE_BASELINE_STRATEGIES),
        "fixed_seed": 42,
        "bohr": {},
        "decision_protocol": {"name": "autonomous-mae-v1"},
    }
    path = tmp_path / "config.toml"
    dump_toml(path, config)
    requests = []

    def prepare(request_path: Path, root: Path) -> Path:
        import tomllib

        request = tomllib.loads(request_path.read_text())
        requests.append(request)
        root.mkdir()
        value = {
            "round_count": 40,
            "batch_size": 40,
            "training": {"epochs": 300},
            "source_revision": "fixed",
            "evaluation_sha256": "gt",
            "validation_panels": {},
            "sar_backend": {"source_revision": "fixed", "method_id": "shared-backend"},
            "private_hashes": {"pair-pool.csv": "pool"},
            "assets_hashes": {"worker.py": "worker"},
            "sampling_experiment": EXPERIMENT_SAMPLING_SCHEMA,
            "fixed_seed": 42,
        }
        (root / "experiment.json").write_text(json.dumps(value))
        for seed in range(5):
            folder = root / f"seed-{seed:02d}"
            folder.mkdir()
            (folder / "state.json").write_text(json.dumps({"selected_ids": [f"{seed}:initial"]}))
        return root

    with (
        patch.object(suite, "prepare", side_effect=prepare),
        patch.object(suite, "stage_round"),
        patch.object(suite, "status"),
    ):
        root = suite.prepare_suite(path, tmp_path / "suite")
    plan = json.loads((root / "suite.json").read_text())
    assert len(plan["experiments"]) == 5
    assert plan["sar_backend"] == {"source_revision": "fixed", "method_id": "shared-backend"}
    assert plan["maximum_sar_jobs"] == 1000
    assert len({json.dumps(x["shared_protocol"], sort_keys=True) for x in plan["experiments"]}) == 1
    normalized = [
        {
            key: value
            for key, value in request.items()
            if key not in ("experiment_id", "sampling_strategy")
        }
        for request in requests
    ]
    assert all(request == normalized[0] for request in normalized)
    assert len({x["sampling_protocol"]["doi"] for x in plan["experiments"]}) == 5


def test_literature_strategy_shares_evaluation_protocol_and_rejects_agent_actions() -> None:
    import pytest
    from tricycle_sampling.replay import mae as replay_mae

    protocol = {
        "name": "autonomous-core-hours-mae-v1",
        "feedback": False,
        "review": False,
        "evaluation_budgets_core_hours": [50000, 100000],
    }
    for strategy in LITERATURE_BASELINE_STRATEGIES:
        assert replay_mae.settings({"sampling_strategy": strategy, "decision_protocol": protocol})
        with pytest.raises(ValueError, match="without agent"):
            replay_mae.settings(
                {"sampling_strategy": strategy, "decision_protocol": {**protocol, "feedback": True}}
            )
