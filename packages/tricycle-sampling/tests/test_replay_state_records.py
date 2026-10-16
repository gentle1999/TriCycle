from pathlib import Path

import pytest
from tricycle_sampling.replay import campaign


def initial_state(seed: Path) -> dict:
    state = {"seed": 0, "round": 2, "phase": "monitor", "revision": 10,
             "selected_ids": ["a", "b"], "new_ids": ["b"], "failures": 0,
             "external_id": "job-2"}
    campaign.save(seed / "state.json", state)
    return state


def test_polling_keeps_one_current_state_and_preserves_old_evidence(tmp_path: Path) -> None:
    state = initial_state(tmp_path)
    historical = tmp_path / "history/00000010.json"
    campaign.save(historical, state)
    original = historical.read_bytes()
    for check in range(5):
        state["next_check"] = 100 + check
        campaign.save_state(tmp_path, state)
    assert campaign.read(tmp_path / "state.json") == state
    assert state["revision"] == 15
    assert state["selected_ids"] == ["a", "b"]
    assert not (tmp_path / "state-events.json").exists()
    assert list((tmp_path / "history").iterdir()) == [historical]
    assert historical.read_bytes() == original


def test_failure_and_recovery_are_recorded_without_sampling_payload(tmp_path: Path) -> None:
    state = initial_state(tmp_path)
    state.update(failures=1, last_error="TimeoutError: status unavailable", next_check=200)
    campaign.save_state(tmp_path, state)
    state.update(failures=0, next_check=300)
    state.pop("last_error")
    campaign.save_state(tmp_path, state)
    state.update(phase="download", remote_result="SUCCEEDED")
    campaign.save_state(tmp_path, state)
    events = campaign.read(tmp_path / "state-events.json")["events"]
    assert len(events) == 3
    assert events[0]["after"]["last_error"] == "TimeoutError: status unavailable"
    assert events[1]["before"]["failures"] == 1
    assert "last_error" not in events[1]["after"]
    assert events[2]["after"]["phase"] == "download"
    for event in events:
        for side in ("before", "after"):
            assert not {"selected_ids", "new_ids", "next_check"} & event[side].keys()
    journal_bytes = (tmp_path / "state-events.json").read_bytes()
    state["next_check"] = 400
    campaign.save_state(tmp_path, state)
    assert (tmp_path / "state-events.json").read_bytes() == journal_bytes
    assert campaign.read(tmp_path / "state.json") == state


def test_interrupted_save_resumes_from_current_state_without_duplicate_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = initial_state(tmp_path)
    state.update(phase="download", remote_result="SUCCEEDED")
    real_save = campaign.save

    def fail_state_write(path: Path, value: object) -> None:
        if path == tmp_path / "state.json":
            raise OSError("interrupted state write")
        real_save(path, value)

    monkeypatch.setattr(campaign, "save", fail_state_write)
    with pytest.raises(OSError, match="interrupted state write"):
        campaign.save_state(tmp_path, state)
    resumed = campaign.read(tmp_path / "state.json")
    assert resumed["phase"] == "monitor"
    assert resumed["revision"] == 10
    monkeypatch.setattr(campaign, "save", real_save)
    resumed.update(phase="download", remote_result="SUCCEEDED")
    campaign.save_state(tmp_path, resumed)
    events = campaign.read(tmp_path / "state-events.json")["events"]
    assert len(events) == 1
    assert events[0]["revision"] == resumed["revision"] == 11
    assert campaign.read(tmp_path / "state.json") == resumed
