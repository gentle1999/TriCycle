"""Budget pairing must not hide partial coverage or choose favorable checkpoints."""

from pathlib import Path
from typing import Any

import pytest
from tricycle_core.io import file_sha256, write_json

from analysis.sampling.language_comparison import paired_budgets, read, report


def snapshot(tracks: dict[int, list[tuple[float, float]]],
             scope: list[Any] | None = None) -> dict[str, Any]:
    return {'evaluation_scope': scope or ['gt', 100, 'kcal/mol', 'revision', 300],
            'points': [{'space': 'autode', 'variant': 'intelligent', 'seed': seed,
                        'round_index': index, 'cost': cost, 'mae': mae}
                       for seed, track in tracks.items()
                       for index, (cost, mae) in enumerate(track)]}


def test_latest_affordable_with_partial_coverage() -> None:
    english = snapshot({0: [(10, 1), (20, 4), (30, 2)], 1: [(10, 8), (15, 7)]})
    chinese = snapshot({0: [(10, 3), (25, 2), (30, 1)], 1: [(10, 6), (30, 4)]})
    pairs, rows = paired_budgets(english, chinese, {'autode': [5, 10, 20, 35]}, {'autode': [0, 1]})
    assert [r['paired_seeds'] for r in rows] == [0, 2, 1, 0]
    assert rows[0]['mean_difference'] is None
    assert rows[1]['mean_difference'] == 0  # (-2 + 2) / 2, paired before aggregation.
    assert rows[1]['sd_difference'] == pytest.approx(8 ** 0.5)
    assert rows[2]['mean_difference'] == 1  # Latest English MAE 4, not best MAE 1.
    assert rows[2]['sd_difference'] is None
    assert rows[2]['coverage'] == 'partial'
    assert pairs[-1]['english_round'] == 1
    assert pairs[-1]['chinese_round'] == 0


def test_scope_serialization_and_mismatch() -> None:
    en = snapshot({0: [(10, 1)]})
    zh = snapshot({0: [(10, 2)]})
    en['evaluation_scope'] = tuple(en['evaluation_scope'])
    assert paired_budgets(en, zh, {'autode': [10]}, {'autode': [0]})[1][0]['mean_difference'] == -1
    zh['evaluation_scope'][0] = 'different-gt'
    with pytest.raises(ValueError, match='evaluation scopes'):
        paired_budgets(en, zh, {'autode': [10]}, {'autode': [0]})


def test_rejects_noncontiguous_or_nonfinite_evidence() -> None:
    en = snapshot({0: [(10, 1), (20, 2)]})
    zh = snapshot({0: [(10, 2)]})
    en['points'][1]['round_index'] = 2
    with pytest.raises(ValueError, match='noncontiguous'):
        paired_budgets(en, zh, {'autode': [10]}, {'autode': [0]})
    en['points'][1]['round_index'] = 1
    en['points'][1]['mae'] = float('nan')
    with pytest.raises(ValueError, match='invalid metric'):
        paired_budgets(en, zh, {'autode': [10]}, {'autode': [0]})


def test_observer_waits_then_reports_blocked_seed_without_changing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sampling import language_comparison as module

    en, zh, suite = (tmp_path / name for name in ('en', 'zh', 'suite'))
    config = {'decision_protocol': {'evaluation_budgets_core_hours': [10, 20]}}
    for path in (en, zh):
        write_json(path / 'experiment.json', config)
    write_json(suite / 'suite.json', {'seeds_per_strategy': 1})
    state_path = en / 'seed-00/state.json'
    write_json(state_path, {'seed': 0, 'round': 0, 'phase': 'monitor'})
    plan_path = tmp_path / 'comparison-plan.json'
    write_json(plan_path, {'schema': 'tricycle.language-comparison/v1', 'experiments': [{
        'project': 'autode', 'root': str(en), 'suite': str(suite),
        'historical_experiment': str(zh),
        'english_experiment_sha256': file_sha256(en / 'experiment.json'),
        'historical_experiment_sha256': file_sha256(zh / 'experiment.json'),
    }]})

    def fake_freeze(root: Path, output: Path) -> dict[str, Any]:
        value = snapshot({0: [(10, 1 if root.name == 'english-plan' else 2)]})
        value.update({'source_hashes': {}, 'alignment': 'No extrapolation.',
                      'accounting': 'Proxy core-hours.',
                      'statuses': [{'space': 'autode', 'seed': 0, 'phase': 'blocked',
                                    'completed_checkpoints': 1}]})
        write_json(output / 'snapshot.json', value)
        return value

    monkeypatch.setattr(module, 'freeze', fake_freeze)
    output = tmp_path / 'report'
    report(plan_path, output)
    assert not (output / 'comparison.json').exists()
    assert 'Waiting' in (output / 'REPORT.md').read_text()
    write_json(state_path, {'seed': 0, 'round': 0, 'phase': 'blocked'})
    state_hash = file_sha256(state_path)
    report(plan_path, output)
    result = read(output / 'comparison.json')
    assert result['all_english_complete'] is False
    assert result['summaries'][0]['mean_difference'] == -1
    assert result['summaries'][1]['paired_seeds'] == 0
    assert file_sha256(state_path) == state_hash
    report(plan_path, output)  # Restart uses serialized snapshots consistently.
    assert read(output / 'comparison.json')['pairs'] == result['pairs']
