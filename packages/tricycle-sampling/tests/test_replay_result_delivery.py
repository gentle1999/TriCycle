from pathlib import Path
from typing import Any

import pytest
from tricycle_sampling.replay.results import publish_training_results


def test_only_results_leave_training_work_directories(tmp_path: Path) -> None:
    work = tmp_path / "work"
    for name in (
        "full-fit/refit-input.pt", "full-fit/model/preprocessed.csv",
        "full-fit/model/checkpoint.pt", "full-fit/train.log",
        "full-fit/model/training_summary.json", "validation-fit/model/checkpoint.pt",
        "validation-fit/model/preprocessed.csv", "validation-train.csv", "validation-labels.csv",
    ):
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    output = tmp_path / "results"
    publish_training_results(work, output)
    names = {p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()}
    assert names == {"full-fit/train.log", "full-fit/model/training_summary.json",
                     "validation-fit/model/checkpoint.pt"}
    assert (work / "full-fit/refit-input.pt").exists()


def test_failed_training_still_publishes_diagnostic_log(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "full-fit").mkdir(parents=True)
    (work / "full-fit/train.log").write_text("training failed")
    (work / "full-fit/refit-input.pt").write_bytes(b"uploaded weights")
    publish_training_results(work, tmp_path / "results")
    assert (tmp_path / "results/full-fit/train.log").read_text() == "training failed"
    assert not (tmp_path / "results/full-fit/refit-input.pt").exists()


def test_reused_checkpoint_is_a_reference_and_reconstructed_locally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import os
    import zipfile

    from tricycle_core.io import file_sha256, write_json
    from tricycle_sampling.replay import campaign, worker
    from tricycle_sar.sampling_runtime import stage

    experiment = tmp_path / "experiment"
    current = experiment / "seed-00/round-0001"
    upload = current / "input"
    upload.mkdir(parents=True)
    backend = stage(upload)
    (upload / "previous.pt").write_bytes(b"previous weights")
    rows = [{"path_id": "r1", "source_mapped_reaction_id": "r1", "source_row_index": "0",
             "rxn_smiles": "CC.C>>CCC", "G_T_activate": "1", "ene_id": "1", "diene_id": "2", "prod_id": "0"}]
    for name in ("training.csv", "evaluation.csv"):
        campaign.write_table(upload / name, rows)
    request = {"input_hashes": {"previous.pt": file_sha256(upload / "previous.pt")},
               "sar_backend": backend, "source_revision": backend["source_revision"],
               "training": {"epochs": 300, "batch_size_paths": 512, "device": "cpu"},
               "round_index": 1, "reuse_checkpoint": True, "need_decision": False,
               "evaluation_sha256": file_sha256(upload / "evaluation.csv")}
    write_json(upload / "request.json", request)
    write_json(experiment / "experiment.json", {"asset_store": str(tmp_path / "store")})

    def no_fit(*args: Any, **kwargs: Any) -> Path:
        raise AssertionError("Reused model must not be retrained")

    def predict(checkpoint: Path, data: Path, out: Path, recipe: dict[str, Any]) -> list[dict[str, str]]:
        assert checkpoint == upload / "previous.pt"
        result = [{**rows[0], "prediction": "1", "target": "1"}]
        campaign.write_table(out, result)
        return result

    monkeypatch.setattr(worker, "fit", no_fit)
    monkeypatch.setattr(worker, "predict", predict)
    cwd, prior_root = Path.cwd(), worker.baseline.ROOT
    try:
        worker.run(upload / "request.json")
    finally:
        os.chdir(cwd)
        worker.baseline.ROOT = prior_root
    output = upload / "outputs/round"
    assert not list(output.rglob("*.pt"))
    report = json.loads((output / "worker-report.json").read_text())
    assert report["output_references"]["checkpoint.pt"]["sha256"] == request["input_hashes"]["previous.pt"]
    (current / "download").mkdir()
    with zipfile.ZipFile(current / "download/results.zip", "w") as archive:
        for path in output.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(upload))
    result = campaign.extract_result(current)
    assert (result / "checkpoint.pt").read_bytes() == b"previous weights"
    assert not list((current / "download").rglob("*.pt"))
    assert campaign.extract_result(current) == result  # Recovery is idempotent.
