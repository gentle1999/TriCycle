from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest
from tricycle_core.io import file_sha256
from tricycle_core.toml import dump_toml

from tricycle_sar.benchmark import write_rows
from tricycle_sar.results import publish_recorded, read, verify


def test_recorded_publication_needs_no_old_executable_backend(
    tmp_path: Path, result_package_factory: Callable[..., Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = result_package_factory()
    dataset, splits = source / 'data/dataset.csv', source / 'data/splits.csv'
    model = 'fingerprint_rf'
    record = tmp_path / 'record.toml'
    dump_toml(record, {'schema': 'tricycle.sar-benchmark/v1', 'folds': 5, 'target_unit': 'kcal/mol',
                      'rows': len(read(dataset)), 'models': [model], 'methods': {model: model + '/train/' + 'a' * 16},
                      'dataset_sha256': file_sha256(dataset), 'splits_sha256': file_sha256(splits),
                      'training_protocol_manifest': '/unavailable/old/catalog.json',
                      'training_protocol_manifest_sha256': 'b' * 64,
                      'row_id': 'path_id', 'target': 'G_T_activate', 'group_columns': ['ene_id', 'diene_id']})
    predictions = tmp_path / 'predictions.csv'
    write_rows(predictions, [{'model': r['model'], 'fold': r['fold'], 'reaction_id': r['path_id'],
                             'row_index': r['row_index'], 'target_name': 'G_T_activate',
                             'y_true': r['reported_y_true'], 'y_pred': r['y_pred']}
                            for r in read(source / 'results/oof-predictions.csv')])
    monkeypatch.setattr('tricycle_core.training_protocol.resolve_training_registry',
                        Mock(side_effect=AssertionError('Must not load old runtime')))
    output = publish_recorded(record, dataset, splits, predictions, tmp_path / 'published')
    assert verify(output)['metrics'][model]['oof_mae'] == 1
    assert 'a' * 16 in (output / 'METHOD_BINDINGS.csv').read_text()
    changed = tmp_path / 'changed.csv'
    changed.write_bytes(dataset.read_bytes() + b'\n')
    with pytest.raises(ValueError, match='data or splits changed'):
        publish_recorded(record, changed, splits, predictions, tmp_path / 'bad-data')
    values = read(predictions)
    write_rows(predictions, values + [values[0]])
    with pytest.raises(ValueError, match='coverage'):
        publish_recorded(record, dataset, splits, predictions, tmp_path / 'bad-oof')
    assert not (tmp_path / 'bad-oof').exists()
