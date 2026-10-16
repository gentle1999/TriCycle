from copy import deepcopy
from pathlib import Path

import pytest
from tricycle_core.io import file_sha256

from tricycle_sar.backends import CATALOG
from tricycle_sar.protocol_identity import compare, current_identity, scientific_source


def test_storage_hook_changes_do_not_change_scientific_shell_identity() -> None:
    before = b'export OMP_NUM_THREADS=2\npreserve_outputs() {\n  cp data results\n}\ntrap preserve_outputs EXIT\n'
    after = before.replace(b'cp data results', b'mv data results')
    assert scientific_source('common/sar_worker_common.sh', before) == scientific_source('common/sar_worker_common.sh', after)
    assert scientific_source('common/sar_worker_common.sh', before) != scientific_source(
        'common/sar_worker_common.sh', after.replace(b'OMP_NUM_THREADS=2', b'OMP_NUM_THREADS=4'))
    with pytest.raises(ValueError, match='boundary changed'):
        scientific_source('common/sar_worker_common.sh', after + b'run_more_training\n')


def test_result_metrics_remain_bound_while_retention_changes_do_not() -> None:
    before = b'''def collect(bundle, task_path, result, exit_code):
    prediction = 3.0
    write(result / "predictions.csv", prediction)
    write(result / "status.csv", [])
    if task.get("retain_checkpoint", False):
        save_checkpoint("old")
'''
    after = before.replace(b'save_checkpoint("old")', b'save_checkpoint("new")')
    assert scientific_source('common/sar_outputs.py', before) == scientific_source('common/sar_outputs.py', after)
    assert scientific_source('common/sar_outputs.py', before) != scientific_source(
        'common/sar_outputs.py', after.replace(b'prediction = 3.0', b'prediction = 4.0'))
    with pytest.raises(ValueError, match='boundary changed'):
        scientific_source('common/sar_outputs.py', before.replace(b'status.csv', b'other.csv'))


def test_unknown_source_and_numerical_environment_are_bound() -> None:
    from tricycle_core.training_protocol import resolve_training_registry

    manifest, registry = resolve_training_registry(CATALOG)
    method = registry['methods'][registry['models']['fingerprint_rf']['default_method']]
    original = current_identity(manifest, method)
    changed = deepcopy(method)
    changed['image'] = 'different-image'
    assert current_identity(manifest, changed)['scientific_sha256'] != original['scientific_sha256']
    changed = deepcopy(method)
    changed['executor'] = 'different-scheduler'
    assert current_identity(manifest, changed)['scientific_sha256'] == original['scientific_sha256']
    assert scientific_source('new/unknown.py', b'x = 1') != scientific_source('new/unknown.py', b'x = 2')
    assert len(original['components']) == len(method['files'])
    assert original['selector_sha256'] == file_sha256(Path(__file__).parents[1] / 'src/tricycle_sar/protocol_identity.py')


def test_comparison_distinguishes_delivery_and_requires_the_same_policy() -> None:
    from tricycle_core.training_protocol import resolve_training_registry

    manifest, registry = resolve_training_registry(CATALOG)
    method = registry['methods'][registry['models']['fingerprint_rf']['default_method']]
    original = current_identity(manifest, method)
    changed = {**original, 'delivery_sha256': '0' * 64}
    assert compare(original, changed) == {'same_scientific_method': True, 'same_delivery': False}
    changed['selector_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='policies differ'):
        compare(original, changed)
