from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
from tricycle_bohr import BohrComputeRequest
from tricycle_bohrsubmit import BatchRequest, DpaBernyRequest, RescueRequest
from tricycle_bohrsubmit.gateway import BohrCliConfig
from tricycle_core import Artifact, BohrJobSpec, ExecutionContext, InvalidRequestError
from tricycle_core.toml import dump_toml, load_toml
from tricycle_exploration import ExplorationRoundRequest, ModelSnapshot, SpaceDefinition
from tricycle_reaction_db.models import (
    CalculationLogBatchSpec,
    ReactionDatabaseProjectSpec,
    ReactionIngestRequest,
    RxnResidDatasetExportRequest,
)
from tricycle_reactionopt.models import ReactionProfileRequest
from tricycle_reactionopt.rits import RitsInferenceConfig
from tricycle_rits import RitsPlugin
from tricycle_rits import assets as rits_assets
from tricycle_sampling import (
    Candidate,
    SamplingRequest,
    decompose_binary_uncertainty,
    get_uncertainty_field,
    get_uncertainty_scores,
)
from tricycle_sar import assets as sar_assets


@pytest.mark.parametrize("document", [
    {"executable": "flat-entry"},
    {"unknown_section": {}},
    {"bohr": {"bin": "old-entry"}},
    {"bohr": {"executable": "bohr", "bin": "conflicting-entry"}},
    {"retry": {"max_atempts": 1}},
    {"retry": {"max_attempts": 3, "max_atempts": 1}},
])
def test_bohr_adapter_rejects_unknown_sections_and_fields(
    tmp_path: Path, document: dict[str, Any],
) -> None:
    path = tmp_path / 'adapter.toml'
    dump_toml(path, document)
    with pytest.raises(InvalidRequestError, match='unknown'):
        BohrCliConfig.from_toml(path)


@pytest.mark.parametrize('module', [sar_assets, rits_assets])
@pytest.mark.parametrize('shape', [
    'flat', 'top-level-extra', 'model-extra', 'missing-model', 'missing-schema', 'wrong-schema',
    'wrong-revision-kind',
])
def test_model_asset_files_require_only_the_declared_model_table(
    tmp_path: Path, module: ModuleType, shape: str,
) -> None:
    document = load_toml(module.DEFAULT_MODEL_MANIFEST)
    model = document['model']
    if shape == 'flat':
        document = model
    elif shape == 'top-level-extra':
        document['unused'] = {'revision': 'ignored'}
    elif shape == 'model-extra':
        model['training_entrypiont'] = 'wrong.py'
    elif shape == 'missing-model':
        del document['model']
    elif shape == 'missing-schema':
        del document['schema']
    elif shape == 'wrong-schema':
        document['schema'] = 'unsupported-model'
    else:
        model['revision_kind'] = 'branch'
    path = tmp_path / 'model.toml'
    dump_toml(path, document)
    with pytest.raises(InvalidRequestError):
        module.load_model_asset(path)


@pytest.mark.parametrize('module', [sar_assets, rits_assets])
def test_model_asset_mapping_and_current_file_round_trip(
    tmp_path: Path, module: ModuleType,
) -> None:
    asset = module.load_model_asset()
    payload = asdict(asset)
    assert type(asset).from_mapping(payload) == asset
    path = tmp_path / 'model.toml'
    schema = load_toml(module.DEFAULT_MODEL_MANIFEST)['schema']
    dump_toml(path, {'schema': schema, 'model': payload})
    assert module.load_model_asset(path) == asset
    with pytest.raises(InvalidRequestError, match='training_entrypiont'):
        type(asset).from_mapping({**payload, 'training_entrypiont': 'wrong.py'})


@pytest.mark.parametrize(
    ("parse", "payload", "field"),
    [
        (BatchRequest.from_mapping, {"out_dir": "correct", "out": "wrong"}, "out"),
        (DpaBernyRequest.from_mapping, {"model": "wrong.pth"}, "model"),
        (RescueRequest.from_mapping, {"source_run": "correct", "run": "wrong"}, "run"),
        (BohrComputeRequest.from_mapping, {"job_name": "correct", "name": "wrong"}, "name"),
        (
            ReactionDatabaseProjectSpec.from_mapping,
            {"slug": "correct", "project_slug": "wrong"},
            "project_slug",
        ),
        (CalculationLogBatchSpec.from_mapping, {"root": "correct", "path": "wrong"}, "path"),
        (
            RxnResidDatasetExportRequest.from_mapping,
            {"project_id": "project", "output": "wrong.csv"},
            "output",
        ),
        (ReactionIngestRequest.from_mapping, {"reaction_id": "flat-record"}, "reaction_id"),
        (ReactionProfileRequest.from_mapping, {"reaction_smiles": "A>>B", "ts": {}}, "ts"),
        (RitsInferenceConfig.from_mapping, {"checkpoint": "ambiguous.ckpt"}, "checkpoint"),
        (
            SamplingRequest.from_mapping,
            {"candidates": [{"candidate_id": "correct", "id": "wrong"}]},
            "id",
        ),
        (
            ExplorationRoundRequest.from_mapping,
            {"exploration_id": "correct", "experiment_id": "wrong"},
            "experiment_id",
        ),
    ],
)
def test_old_fields_fail_before_defaults_or_asset_loading(
    parse: Callable[[Mapping[str, Any]], object], payload: dict[str, Any], field: str
) -> None:
    with (
        patch("tricycle_bohr.models.load_dotenv", side_effect=AssertionError("read environment")),
        patch(
            "tricycle_bohrsubmit.models.load_dotenv", side_effect=AssertionError("read environment")
        ),
        patch(
            "tricycle_reactionopt.rits.load_model_asset", side_effect=AssertionError("read assets")
        ),
        pytest.raises(InvalidRequestError, match=field),
    ):
        parse(payload)


def test_planned_bohr_job_round_trip_never_reads_request_defaults() -> None:
    job = BohrJobSpec(
        "planned",
        "train",
        "image",
        ("bash", "run.sh"),
        input_artifacts=(Artifact("code", "/inputs", kind="directory"),),
        parameters={"fold": 2},
        tags=("sar",),
    )
    with patch("tricycle_bohr.models.load_dotenv", side_effect=AssertionError("read environment")):
        assert BohrJobSpec.from_mapping(job.to_dict()) == job
    with pytest.raises(InvalidRequestError, match="job_name"):
        BohrJobSpec.from_mapping({**job.to_dict(), "job_name": "conflicting"})


@pytest.mark.parametrize("operation", ["fine-tune", "fine_tune", "fine-tuning", "fine_tuning"])
def test_model_operations_only_accept_canonical_names(operation: str) -> None:
    with pytest.raises(InvalidRequestError, match="unsupported rits operation"):
        RitsPlugin().plan(operation, {}, ExecutionContext("reject-alias"))


def test_exploration_request_round_trip_keeps_training_and_history(tmp_path: Path) -> None:
    request = ExplorationRoundRequest(
        exploration_id="contract",
        round_index=2,
        candidates=(Candidate("selected"), Candidate("excluded")),
        budget=1,
        space=SpaceDefinition("pool", definition={"reaction_class": "DA"}),
        model_snapshot=ModelSnapshot("model", checkpoint_hash="sha256:checkpoint"),
        last_training_round=1,
        new_label_count=5,
        previous_checkpoint="sha256:checkpoint",
        training_action="skip",
        seen_candidate_ids=("seen",),
        exclude_seen=True,
        excluded_candidates={"excluded": "invalid"},
        output_dir=tmp_path,
    )
    restored = ExplorationRoundRequest.from_mapping(request.to_dict())
    assert restored == request
    assert restored.request_hash == request.request_hash
    for old_field in ("new_label_count", "training_action", "seen_candidate_ids", "run_dir"):
        with pytest.raises(InvalidRequestError, match=old_field):
            ExplorationRoundRequest.from_mapping({**request.to_dict(), old_field: "old"})


@pytest.mark.parametrize(
    "old_field", ["predictive_unc", "predictive", "uncertainty", "posterior_probs"]
)
def test_old_uncertainty_field_cannot_be_hidden_by_a_canonical_score(old_field: str) -> None:
    candidate = Candidate("candidate", {"predictive_variance": 0.5, old_field: 100})
    with pytest.raises(InvalidRequestError, match=old_field):
        get_uncertainty_field(candidate, "predictive")
    with pytest.raises(InvalidRequestError, match=old_field):
        get_uncertainty_scores(candidate)


def test_entropy_scores_round_trip_without_changing_units() -> None:
    scores = decompose_binary_uncertainty([0.2, 0.8])
    assert get_uncertainty_scores(Candidate("candidate", scores.to_dict())) == scores


def test_rits_inference_config_round_trip_keeps_checkpoint_and_command() -> None:
    config = RitsInferenceConfig.from_mapping(
        {
            "checkpoint_artifact": {"artifact_id": "weights", "location": "/weights/source.ckpt"},
            "checkpoint_path": "inputs/weights.ckpt",
        }
    )
    restored = RitsInferenceConfig.from_mapping(config.to_dict())
    assert restored == config
    assert restored.checkpoint == "inputs/weights.ckpt"
