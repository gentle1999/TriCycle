import json
from pathlib import Path

import numpy as np
import pytest
from tricycle_core.io import file_sha256, write_json
from tricycle_sampling.replay.published import checksum_inventory, table, verify


def package(tmp_path: Path) -> Path:
    root = tmp_path / 'report'
    table(root / 'data/evaluation.csv', [{'path_id': 'a', 'G_T_activate': 1}, {'path_id': 'b', 'G_T_activate': 2}])
    table(root / 'data/pools/autode.csv', [{'pair_id': 'p', 'materialized_sampling_cost_core_hours': 5}])
    table(root / 'data/checkpoints.csv', [
        {'cohort': cohort, 'space': 'autode', 'variant': 'intelligent', 'seed': 0, 'round_index': 0,
         'cost': 5, 'mae': 1, 'rmse': 1, 'bias': 1, 'n_gt': 2, 'sampled_pairs': 1}
        for cohort in ('chinese', 'english')])
    table(root / 'data/coverage.csv', [
        {'cohort': cohort, 'space': 'autode', 'variant': 'intelligent', 'seed': 0, 'phase': 'complete',
         'completed_checkpoints': 1, 'expected_checkpoints': 1} for cohort in ('chinese', 'english')])
    (root / 'data/selections.jsonl').write_text('\n'.join(json.dumps({'new_ids': ['p']}) for _ in range(2)) + '\n')
    np.savez_compressed(root / 'data/evaluation-predictions.npz', predictions=[[2., 3.], [2., 3.]], targets=[1., 2.])
    write_json(root / 'manifest.json', {'schema': 'tricycle.replay.published/v1', 'protocols': [],
               'evaluation_scope': [file_sha256(root / 'data/evaluation.csv')], 'trajectories': 2, 'checkpoints': 2})
    checksum_inventory(root)
    return root


def test_observations_recompute_without_source_runs(tmp_path: Path) -> None:
    root = package(tmp_path)
    assert verify(root)['prediction_values'] == 4


@pytest.mark.parametrize('change', ['prediction', 'cost', 'round', 'selection', 'target'])
def test_recomputed_science_detects_inconsistency_even_with_refreshed_file_hashes(tmp_path: Path, change: str) -> None:
    root = package(tmp_path)
    if change in {'prediction', 'target'}:
        np.savez_compressed(root / 'data/evaluation-predictions.npz',
                            predictions=[[2., 4.], [2., 3.]] if change == 'prediction' else [[2., 3.], [2., 3.]],
                            targets=[1., 3.] if change == 'target' else [1., 2.])
    elif change == 'selection':
        (root / 'data/selections.jsonl').write_text('{"new_ids":["p","p"]}\n{"new_ids":["p"]}\n')
    else:
        from tricycle_sampling.replay.published import rows
        values = rows(root / 'data/checkpoints.csv')
        values[0]['cost' if change == 'cost' else 'round_index'] = '10'
        table(root / 'data/checkpoints.csv', values)
    checksum_inventory(root)
    with pytest.raises(ValueError):
        verify(root)
