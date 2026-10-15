from pathlib import Path

import pytest
from tricycle_sampling.replay import campaign


def test_storage_transition_keeps_original_binding_and_rejects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    module = source / "controller.py"
    module.write_text("original")
    original = campaign.digest(module)
    run = tmp_path / "run"
    run.mkdir()
    config = run / "experiment.json"
    campaign.save(config, {"controller_hashes": {"controller.py": original}})
    monkeypatch.setattr(campaign, "PACKAGE_ROOT", source)
    campaign.check_controller(run)
    module.write_text("storage change")
    with pytest.raises(ValueError, match="frozen artifact"):
        campaign.check_controller(run)
    evidence = tmp_path / "audit.json"
    evidence.write_text("audited storage equivalence")
    record: dict = {"schema": "tricycle.replay-storage-compatibility/v1",
              "experiment_sha256": campaign.digest(config),
              "authorization": "explicit user instruction", "reason": "storage only",
              "evidence_path": str(evidence), "evidence_sha256": campaign.digest(evidence),
              "changes": {"controller.py": {"before": original, "after": campaign.digest(module)}}}
    campaign.save(run / "storage-compatibility.json", record)
    campaign.check_controller(run)
    frozen = config.read_bytes()
    module.write_text("unreviewed change")
    with pytest.raises(ValueError, match="frozen artifact"):
        campaign.check_controller(run)
    assert config.read_bytes() == frozen
    record["changes"]["controller.py"]["before"] = "wrong"
    campaign.save(run / "storage-compatibility.json", record)
    with pytest.raises(ValueError, match="original controller"):
        campaign.check_controller(run)


def test_storage_transition_rejects_changed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(campaign, "PACKAGE_ROOT", tmp_path)
    campaign.save(tmp_path / "experiment.json", {"controller_hashes": {}})
    evidence = tmp_path / "audit"
    evidence.write_text("first")
    campaign.save(tmp_path / "storage-compatibility.json", {
        "schema": "tricycle.replay-storage-compatibility/v1",
        "experiment_sha256": campaign.digest(tmp_path / "experiment.json"),
        "authorization": "user", "reason": "storage only",
        "evidence_path": str(evidence), "evidence_sha256": campaign.digest(evidence), "changes": {},
    })
    evidence.write_text("changed")
    with pytest.raises(ValueError, match="evidence changed"):
        campaign.check_controller(tmp_path)
