from pathlib import Path
from unittest.mock import patch

import pytest
from tricycle_core.io import atomic_write_text, copy_snapshot_tree
from tricycle_core.metrics import regression_metrics
from tricycle_core.toml import dump_toml, load_toml


def test_atomic_failure_keeps_existing_record_and_cleans_temporary(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("accepted")
    with patch("os.replace", side_effect=OSError("interrupted")), pytest.raises(OSError):
        atomic_write_text(path, "new")
    assert path.read_text() == "accepted"
    assert list(tmp_path.iterdir()) == [path]


def test_literal_toml_keys_and_control_characters_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "record.toml"
    record = {"source.revision": "a", "文件/名称": {"scripts/a.py": 'x\t\n\x01"\\'}}
    dump_toml(path, record)
    assert load_toml(path) == record
    with pytest.raises(ValueError):
        dump_toml(path, {"nan": float("nan")})
    assert load_toml(path) == record


def test_constant_target_and_nonfinite_metrics() -> None:
    assert regression_metrics([2, 2], [3, 3]) == {"n": 2, "mae": 1, "rmse": 1, "bias": 1}
    with pytest.raises(ValueError, match="non-finite"):
        regression_metrics([2], [float("nan")])
    with pytest.raises(ValueError, match="count"):
        regression_metrics([2], [])


def test_source_snapshots_remain_independent_of_mutable_source(tmp_path: Path) -> None:
    source, frozen = tmp_path / "source", tmp_path / "frozen"
    source.mkdir()
    (source / "code.py").write_text("old")
    copy_snapshot_tree(source, frozen)
    assert (source / "code.py").stat().st_ino != (frozen / "code.py").stat().st_ino
    (source / "code.py").write_text("new")
    assert (frozen / "code.py").read_text() == "old"
