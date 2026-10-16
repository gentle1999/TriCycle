"""Observe local replay results and compare matched language-policy cohorts."""

from __future__ import annotations

import argparse
import fcntl
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from tricycle_core.io import atomic_write_text, file_sha256, write_delimited, write_json

from analysis.sampling.replay_sampling_panels import freeze, read, summarize


def paired_budgets(english: dict, chinese: dict, budgets: dict, seeds: dict) -> tuple[list, list]:
    """Use the latest affordable checkpoint, only inside both observed domains."""
    if english['evaluation_scope'] is not None and (
        tuple(english['evaluation_scope']) != tuple(chinese['evaluation_scope'] or ())
    ):
        raise ValueError('Language cohorts have different evaluation scopes')
    tracks = defaultdict(list)
    for language, snapshot in [('english', english), ('chinese', chinese)]:
        summarize(snapshot['points'])  # Validate contiguous rounds, finite metrics and costs.
        for point in snapshot['points']:
            if point['variant'] != 'intelligent':
                raise ValueError('Language comparison requires intelligent trajectories')
            tracks[language, point['space'], point['seed']].append(point)
    for track in tracks.values():
        track.sort(key=lambda p: p['round_index'])
    pairs, summaries = [], []
    for space, nodes in budgets.items():
        for budget in nodes:
            if not math.isfinite(budget) or budget < 0:
                raise ValueError('Invalid evaluation budget')
            local = []
            for seed in seeds[space]:
                chosen = {}
                for language in ('english', 'chinese'):
                    track = tracks[language, space, seed]
                    if track and track[0]['cost'] <= budget <= track[-1]['cost']:
                        chosen[language] = next(p for p in reversed(track) if p['cost'] <= budget)
                if len(chosen) != 2:
                    continue
                en, zh = chosen['english'], chosen['chinese']
                row = {'space': space, 'budget_core_hours': budget, 'seed': seed,
                       'english_round': en['round_index'], 'chinese_round': zh['round_index'],
                       'english_cost': en['cost'], 'chinese_cost': zh['cost'],
                       'english_mae': en['mae'], 'chinese_mae': zh['mae'],
                       'difference': en['mae'] - zh['mae']}
                local.append(row)
                pairs.append(row)
            differences = [p['difference'] for p in local]
            summaries.append({
                'space': space, 'budget_core_hours': budget, 'paired_seeds': len(local),
                'expected_seeds': len(seeds[space]),
                'coverage': 'complete' if len(local) == len(seeds[space]) else 'partial',
                'english_mean_mae': statistics.mean(p['english_mae'] for p in local) if local else None,
                'chinese_mean_mae': statistics.mean(p['chinese_mae'] for p in local) if local else None,
                'mean_difference': statistics.mean(differences) if local else None,
                'sd_difference': statistics.stdev(differences) if len(local) > 1 else None,
            })
    return pairs, summaries


def cohort_status(plan: dict) -> list:
    rows = []
    for item in plan['experiments']:
        root = Path(item['root'])
        count = read(Path(item['suite']) / 'suite.json')['seeds_per_strategy']
        paths = sorted(root.glob('seed-*/state.json'))
        states = [read(path) for path in paths]
        if sorted(s['seed'] for s in states) != list(range(count)):
            raise ValueError('Missing or duplicated seed states')
        for path, state in zip(paths, states, strict=True):
            rows.append({'space': item['project'], 'seed': state['seed'],
                         'phase': state['phase'], 'round': state['round'],
                         'error': state.get('last_error', ''),
                         'state_path': str(path), 'state_sha256': file_sha256(path)})
    if not rows:
        raise ValueError('Empty comparison cohort')
    return rows


def frozen_cohort(root: Path, output: Path) -> dict:
    """Reuse immutable snapshots only while their evidence hashes still match."""
    path = output / 'snapshot.json'
    if not path.exists():
        return freeze(root, output)
    snapshot = read(path)
    for source, expected in snapshot['source_hashes'].items():
        if file_sha256(Path(source)) != expected:
            raise ValueError(f'Frozen comparison evidence changed: {source}')
    return snapshot


def intervention_records(plan: dict) -> list:
    """Disclose authorized mid-run repairs separately from the original language policy."""
    records = []
    for item in plan['experiments']:
        root = Path(item['root'])
        paths = [root / 'decision-controller-amendment.json',
                 *sorted(root.glob('seed-*/round-*/decision/recovery-authorization.json'))]
        for path in paths:
            if not path.exists():
                continue
            value = read(path)
            if ('evidence_path' in value
                    and file_sha256(Path(value['evidence_path'])) != value['evidence_sha256']):
                raise ValueError('Decision intervention evidence changed')
            records.append({'space': item['project'], 'path': str(path),
                            'sha256': file_sha256(path), 'record': value})
    return records


def report(plan_path: Path, output: Path, *, watch: bool = False, poll_seconds: float = 60) -> None:
    if poll_seconds < 1 or not math.isfinite(poll_seconds):
        raise ValueError('poll_seconds must be finite and at least one')
    plan = read(plan_path)
    if plan['schema'] != 'tricycle.language-comparison/v1':
        raise ValueError('Unsupported comparison plan')
    plan_hash = file_sha256(plan_path)
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        binding = output / 'plan-binding.json'
        if binding.exists() and read(binding)['sha256'] != plan_hash:
            raise ValueError('Comparison plan changed after reporting started')
        write_json(binding, {'path': str(plan_path), 'sha256': plan_hash})
        budgets, seeds, historical, current = {}, {}, [], []
        for item in plan['experiments']:
            space = item['project']
            if space in budgets:
                raise ValueError('Duplicate comparison space')
            en_path = Path(item['root']) / 'experiment.json'
            zh_path = Path(item['historical_experiment']) / 'experiment.json'
            for path, key in [(en_path, 'english_experiment_sha256'),
                              (zh_path, 'historical_experiment_sha256')]:
                if file_sha256(path) != item[key]:
                    raise ValueError(f'Comparison configuration changed: {path}')
            en, zh = read(en_path), read(zh_path)
            budgets[space] = en['decision_protocol']['evaluation_budgets_core_hours']
            if budgets[space] != zh['decision_protocol']['evaluation_budgets_core_hours']:
                raise ValueError('Evaluation budgets differ between cohorts')
            count = read(Path(item['suite']) / 'suite.json')['seeds_per_strategy']
            seeds[space] = list(range(count))
            historical.append({'project': space, 'variant': 'intelligent',
                               'root': item['historical_experiment'],
                               'config_sha256': item['historical_experiment_sha256']})
            current.append({'project': space, 'variant': 'intelligent', 'root': item['root'],
                            'config_sha256': item['english_experiment_sha256']})
        reference = output / 'historical-plan'
        write_json(reference / 'sampling-plan.json', {'experiments': historical})
        current_plan = output / 'english-plan'
        write_json(current_plan / 'sampling-plan.json', {'experiments': current})
        chinese = frozen_cohort(reference, output / 'chinese-snapshot')
        previous = None
        while True:
            if file_sha256(plan_path) != plan_hash:
                raise ValueError('Comparison plan changed while observing')
            states = cohort_status(plan)
            terminal = all(s['phase'] in ('complete', 'blocked') for s in states)
            signature = [(s['space'], s['seed'], s['phase'], s['round'], s['error']) for s in states]
            if signature != previous:
                write_json(output / 'status.json', {'at': datetime.now().astimezone().isoformat(),
                           'terminal': terminal, 'states': states})
                print(f"{sum(s['phase'] == 'complete' for s in states)}/{len(states)} complete; "
                      f"{sum(s['phase'] == 'blocked' for s in states)} blocked", flush=True)
                previous = signature
            if terminal:
                english = frozen_cohort(current_plan, output / 'english-snapshot')
                pairs, summaries = paired_budgets(english, chinese, budgets, seeds)
                result = {'schema': 'tricycle.language-comparison-results/v1',
                          'plan_sha256': plan_hash, 'states': states,
                          'historical_statuses': chinese['statuses'],
                          'interventions': intervention_records(plan),
                          'all_english_complete': all(s['phase'] == 'complete' for s in states),
                          'pairs': pairs, 'summaries': summaries,
                          'snapshot_hashes': {name: file_sha256(output / f'{name}-snapshot/snapshot.json')
                                              for name in ('english', 'chinese')},
                          'analysis_sha256': file_sha256(Path(__file__)),
                          'alignment': english['alignment'], 'accounting': english['accounting']}
                write_delimited(output / 'paired-seeds.csv',
                                ['space', 'budget_core_hours', 'seed', 'english_round', 'chinese_round',
                                 'english_cost', 'chinese_cost', 'english_mae', 'chinese_mae', 'difference'], pairs)
                write_delimited(output / 'budget-summary.csv', list(summaries[0]), summaries)
                write_json(output / 'comparison.json', result)
                write_report(output, result)
                return
            atomic_write_text(output / 'REPORT.md',
                              '# English versus Chinese sampling policies\n\n'
                              'Waiting for the English trajectories to finish or become blocked. '
                              'The historical reference snapshot is frozen. See status.json for progress.\n')
            if not watch:
                return
            time.sleep(poll_seconds)


def write_report(output: Path, result: dict) -> None:
    def fmt(value: float | None) -> str:
        return '—' if value is None else f'{value:.4f}'

    lines = ['# English versus Chinese sampling policies', '',
             'All English trajectories complete: ' + str(result['all_english_complete']) + '.', '',
             'GT MAE is in kcal/mol. Differences are English minus Chinese; negative favors English. '
             'Means use the same paired seeds. SD is the sample SD of paired differences, not a confidence interval.', '',
             '| Space | Core-hour budget | Paired seeds | English MAE | Chinese MAE | Difference | SD |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for row in result['summaries']:
        lines.append(f"| {row['space']} | {row['budget_core_hours']:g} | "
                     f"{row['paired_seeds']}/{row['expected_seeds']} | "
                     f"{fmt(row['english_mean_mae'])} | {fmt(row['chinese_mean_mae'])} | "
                     f"{fmt(row['mean_difference'])} | {fmt(row['sd_difference'])} |")
    lines.extend(['', result['alignment'], '', result['accounting'], '',
                  'Cells with fewer than all seeds are partial descriptive comparisons. '
                  'Blocked seeds remain visible and no missing outcome is imputed.', '',
                  'The historical prompts already contained English instructions but requested Chinese narratives. '
                  'The new policy requests English analysis and narratives and changes narrative length limits. '
                  'Storage/reporting infrastructure and launch scheduling also differ. '
                  'Model-service variability is uncontrolled. This matched policy comparison does not isolate '
                  'a causal effect of language alone.', '', '## Trajectory coverage', '',
                  '| Cohort | Space | Seed | Status | Completed checkpoints |',
                  '| --- | --- | ---: | --- | ---: |'])
    english = read(output / 'english-snapshot/snapshot.json')
    for cohort, statuses in [('English', english['statuses']), ('Chinese', result['historical_statuses'])]:
        for state in statuses:
            lines.append(f"| {cohort} | {state['space']} | {state['seed']} | {state['phase']} | "
                         f"{state['completed_checkpoints']} |")
    if result.get('interventions'):
        lines.extend(['', '## Authorized execution repairs', '',
                      'The trajectories listed below include authorized execution recovery, decision-prompt repairs, or '
                      'additional bounded correction attempts. Their full trajectories must not be '
                      'described as an unchanged language-only treatment. Original failures and '
                      'configuration bindings are preserved; receipts identify intervention scope.', ''])
        for item in result['interventions']:
            reason = item['record'].get('reason') or item['record'].get('original_error') or item['record']['authorization']
            lines.append(f"- {item.get('cohort', 'english')} / {item['space']}: {reason} "
                         f"Receipt: `{item['path']}`; SHA-256: `{item['sha256']}`.")
        recovered = [item for item in result['interventions'] if item.get('prompt_language_after_recovery') == 'english'
                     and item.get('cohort') == 'chinese']
        if recovered:
            lines.extend(['', '### Historical cohort recovery boundaries', '',
                          'These historical trajectories continue with English decisions after recovery. '
                          'Their complete curves are mixed-policy trajectories, not pure Chinese controls. '
                          'The pre-recovery report remains the unchanged historical comparison.', '',
                          '| Space | Seed | First recovered decision round (zero-based) |',
                          '| --- | ---: | ---: |'])
            for item in recovered:
                lines.append(f"| {item['space']} | {item['seed']} | {item['first_recovered_round']} |")
    atomic_write_text(output / 'REPORT.md', '\n'.join(lines) + '\n')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--poll-seconds', type=float, default=60)
    args = parser.parse_args()
    report(args.plan.resolve(), args.out_dir.resolve(), watch=args.watch, poll_seconds=args.poll_seconds)


if __name__ == '__main__':
    main()
