from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from tricycle_core.io import file_sha256
from tricycle_sar.backends.complete_runner import (
    read_frozen_snapshot,
)


def snapshot(root: Path, ids: list[str], declared_rows: object | None = None) -> Path:
    folder = root / "data/snapshots/ground-truth"
    folder.mkdir(parents=True)
    dataset = folder / "dataset.csv"
    with dataset.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["path_id"])
        writer.writerows([ident] for ident in ids)
    (folder / "manifest.json").write_text(json.dumps({
        "row_count": len(ids) if declared_rows is None else declared_rows,
        "dataset": {"sha256": file_sha256(dataset)},
    }))
    return dataset


@pytest.mark.parametrize("size", [1, 7, 3021, 3252, 4000])
def test_accepts_any_nonempty_snapshot_size(tmp_path: Path, size: int) -> None:
    snapshot(tmp_path, [f"path-{index}" for index in range(size)])
    assert len(read_frozen_snapshot(tmp_path)) == size


def test_rejects_changed_dataset_bytes(tmp_path: Path) -> None:
    dataset = snapshot(tmp_path, ["one", "two"])
    dataset.write_text(dataset.read_text() + "three\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        read_frozen_snapshot(tmp_path)


def test_rejects_replaced_record_even_when_row_count_is_unchanged(tmp_path: Path) -> None:
    dataset = snapshot(tmp_path, ["one", "two"])
    dataset.write_text(dataset.read_text().replace("two", "other"))
    with pytest.raises(ValueError, match="hash mismatch"):
        read_frozen_snapshot(tmp_path)


def test_rejects_missing_path_id_cell(tmp_path: Path) -> None:
    dataset = snapshot(tmp_path, ["one"])
    dataset.write_text("other,path_id\nx\n")
    manifest = dataset.with_name("manifest.json")
    value = json.loads(manifest.read_text())
    value["dataset"]["sha256"] = file_sha256(dataset)
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="path ids"):
        read_frozen_snapshot(tmp_path)


def test_rejects_missing_rows_even_with_a_matching_hash(tmp_path: Path) -> None:
    snapshot(tmp_path, ["one"], declared_rows=2)
    with pytest.raises(ValueError, match="row count mismatch"):
        read_frozen_snapshot(tmp_path)


@pytest.mark.parametrize("ids", [["one", "one"], ["one", ""], ["one", " "]])
def test_rejects_ambiguous_profile_identity(tmp_path: Path, ids: list[str]) -> None:
    snapshot(tmp_path, ids)
    with pytest.raises(ValueError, match="path ids"):
        read_frozen_snapshot(tmp_path)


@pytest.mark.parametrize("count", [True, 0, -1, "2", 2.0])
def test_rejects_invalid_declared_count(tmp_path: Path, count: object) -> None:
    snapshot(tmp_path, ["one", "two"], declared_rows=count)
    with pytest.raises(ValueError, match="positive integer"):
        read_frozen_snapshot(tmp_path)
