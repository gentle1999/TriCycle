"""Rebuild sampling figures and paired language comparisons from portable observations."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib  # pyright: ignore[reportMissingImports, reportMissingModuleSource]

matplotlib.use('Agg')
import matplotlib.pyplot as plt  # pyright: ignore[reportMissingImports, reportMissingModuleSource]
import numpy as np
from tricycle_core.io import file_sha256, write_json
from tricycle_sampling.replay.published import read, rows, table, verify

from analysis.sampling.language_comparison import paired_budgets, write_report
from analysis.sampling.replay_sampling_panels import SPACES, summarize


def rebuild(package: Path, output: Path) -> dict:
    verification = verify(package)
    output.mkdir(parents=True, exist_ok=False)
    manifest = read(package / 'manifest.json')
    points = rows(package / 'data/checkpoints.csv')
    statuses = rows(package / 'data/coverage.csv')
    for index, point in enumerate(points):
        point['evaluation_file'] = f'data/evaluation-predictions.npz#row={index}'
        for key in ('seed', 'round_index', 'n_gt', 'sampled_pairs'):
            point[key] = int(point[key])
        for key in ('cost', 'mae', 'rmse', 'bias'):
            point[key] = float(point[key])
    for state in statuses:
        for key in ('seed', 'completed_checkpoints', 'expected_checkpoints'):
            state[key] = int(state[key])
    snapshots = {}
    alignment = 'Latest affordable checkpoint inside both observed seed domains; no extrapolation or interpolation.'
    for cohort in ('chinese', 'english'):
        local = [p for p in points if p['cohort'] == cohort]
        snapshot = {'evaluation_scope': manifest['evaluation_scope'], 'points': local,
                    'statuses': [s for s in statuses if s['cohort'] == cohort],
                    'snapshot_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                    'source_hashes': {str(package / 'SHA256SUMS'): file_sha256(package / 'SHA256SUMS')},
                    'alignment': alignment, 'accounting': manifest['cost']}
        write_json(output / f'{cohort}-all/snapshot.json', snapshot)
        selected = {**snapshot, 'points': [p for p in local if p['variant'] == 'intelligent'],
                    'statuses': [s for s in snapshot['statuses'] if s['variant'] == 'intelligent']}
        write_json(output / f'{cohort}-snapshot/snapshot.json', selected)
        snapshots[cohort] = selected
        summaries, _ = summarize(local)
        table(output / f'{cohort}-all/curve-data.csv', summaries)
    budgets, seeds, protocol_differences = {}, {}, []
    configurations = {}
    for entry in manifest['protocols']:
        if entry['variant'] != 'intelligent':
            continue
        config = read(package / entry['path'])
        configurations[entry['cohort'], entry['space']] = config
    for space in SPACES:
        english, chinese = configurations['english', space], configurations['chinese', space]
        budgets[space] = english['decision_protocol']['evaluation_budgets_core_hours']
        if budgets[space] != chinese['decision_protocol']['evaluation_budgets_core_hours']:
            raise ValueError('Language budgets differ')
        for field in ('training', 'batch_size', 'round_count', 'source_revision', 'evaluation_sha256', 'private_hashes'):
            if english[field] != chinese[field]:
                raise ValueError(f'Language scientific binding differs: {field}')
        protocol_differences.append({'space': space, 'different_fields': sorted(k for k in english.keys() | chinese.keys()
                                                                               if english.get(k) != chinese.get(k))})
        seeds[space] = sorted(s['seed'] for s in snapshots['english']['statuses'] if s['space'] == space)
    pairs, summaries = paired_budgets(snapshots['english'], snapshots['chinese'], budgets, seeds)
    table(output / 'paired-seeds.csv', pairs)
    table(output / 'budget-summary.csv', summaries)
    table(output / 'coverage.csv', statuses)
    intervention_path = package / 'protocols/interventions.json'
    interventions = read(intervention_path)['records'] if intervention_path.exists() else []
    result = {'schema': 'tricycle.language-comparison-results/v1', 'all_english_complete':
              all(s['phase'] == 'complete' for s in snapshots['english']['statuses']),
              'historical_statuses': snapshots['chinese']['statuses'], 'pairs': pairs, 'summaries': summaries,
              'states': snapshots['english']['statuses'], 'interventions': interventions,
              'alignment': alignment, 'accounting': manifest['cost'], 'protocol_differences': protocol_differences,
              'verification': verification, 'analysis_sha256': file_sha256(Path(__file__)),
              'package_inventory_sha256': file_sha256(package / 'SHA256SUMS')}
    write_json(output / 'comparison.json', result)
    write_report(output, result)
    grid = {space: sorted({p['cost'] for cohort in snapshots.values() for p in cohort['points'] if p['space'] == space})
            for space in SPACES}
    _, curves = paired_budgets(snapshots['english'], snapshots['chinese'], grid, seeds)
    table(output / 'paired-curve-data.csv', curves)
    recovered = any(item.get('cohort') == 'chinese' and item.get('prompt_language_after_recovery') == 'english'
                    for item in interventions)
    plot(curves, output, historical_recovered=recovered)
    return {'verification': verification, 'paired_budget_cells': len(summaries),
            'complete_budget_cells': sum(s['coverage'] == 'complete' for s in summaries)}


def plot(curves: list[dict], output: Path, *, historical_recovered: bool = False) -> None:
    with plt.rc_context({'font.family': 'DejaVu Sans', 'font.size': 9, 'axes.spines.top': False,
                         'axes.spines.right': False, 'svg.fonttype': 'none', 'pdf.fonttype': 42}):
        fig, axes = plt.subplots(3, 3, figsize=(12, 8), sharex='col', layout='constrained',
                                gridspec_kw={'height_ratios': [2.2, 1.5, 0.7]})
        for column, (space, title) in enumerate(SPACES.items()):
            local = [r for r in curves if r['space'] == space and r['paired_seeds']]
            x = np.array([r['budget_core_hours'] for r in local]) / 1000
            n = np.array([r['paired_seeds'] for r in local])
            for cohort, color in [('english', '#007D79'), ('chinese', '#8056A0')]:
                y = np.array([r[f'{cohort}_mean_mae'] for r in local])
                label = 'Historical (recovered)' if cohort == 'chinese' and historical_recovered else cohort.title()
                axes[0, column].plot(x, y, color=color, linestyle=':', linewidth=1.5, label=label, drawstyle='steps-post')
                axes[0, column].plot(x, np.where(n == 5, y, np.nan), color=color, linewidth=1.7, drawstyle='steps-post')
            delta = np.array([r['mean_difference'] for r in local])
            sd = np.array([r['sd_difference'] if r['sd_difference'] is not None else np.nan for r in local])
            axes[1, column].plot(x, delta, color='#334155', linestyle=':', linewidth=1, drawstyle='steps-post')
            axes[1, column].plot(x, np.where(n == 5, delta, np.nan), color='#334155', linewidth=1.2, drawstyle='steps-post')
            axes[1, column].fill_between(x, delta - sd, delta + sd, color='#64748B', alpha=0.18, step='post')
            axes[1, column].axhline(0, color='#A3A3A3', linewidth=0.8)
            axes[2, column].step(x, n, where='post', color='#475569', linewidth=1.2)
            axes[2, column].set_ylim(0, 5.5)
            axes[2, column].set_yticks([0, 3, 5])
            axes[2, column].set_xlabel('TS sampling cost (10³ core-hours)')
            axes[0, column].set_title(title, loc='left')
            axes[0, column].set_yscale('log')
            for row in range(3):
                axes[row, column].grid(axis='y', alpha=0.15)
        axes[0, 0].set_ylabel('Paired mean GT MAE (kcal/mol)')
        axes[1, 0].set_ylabel('English − historical MAE' if historical_recovered else 'English − Chinese MAE')
        axes[2, 0].set_ylabel('Paired seeds')
        axes[0, 0].legend(frameon=False)
        fig.suptitle('English versus recovered historical sampling policies' if historical_recovered
                     else 'English versus historical Chinese sampling policies', fontsize=13)
        for extension in ('png', 'pdf', 'svg'):
            fig.savefig(output / f'language-comparison.{extension}', dpi=220, facecolor='white')
        plt.close(fig)
    (output / 'figure-caption.txt').write_text(
        'Means use identical paired seeds at each observed cost. Solid segments have all five pairs; '
        'dotted segments have fewer. Lower panels show English minus Chinese MAE and ±1 sample SD '
        'of paired differences, with explicit pair counts. No interpolation, extrapolation, imputation, '
        'or best-so-far selection is used. The MAE axis is logarithmic; arithmetic summaries use raw values. '
        'The x-axis excludes model/agent overhead. This is a policy comparison with execution interventions '
        'and uncontrolled model-service variability, not a causal estimate of language alone. '
        + ('Recovered historical intelligent trajectories use English decisions after the documented recovery '
           'boundaries; these are mixed-policy curves, not pure Chinese controls. ' if historical_recovered else '') + '\n')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result-dir', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(rebuild(args.result_dir.resolve(), args.out_dir.resolve()), indent=2))


if __name__ == '__main__':
    main()
