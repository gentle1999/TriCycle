"""Prepare and supervise matched MAE-first agent strategies, dry-run by default."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import plistlib
import statistics
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from tricycle_core import record_io as rio
from tricycle_core import timestamped_run_directory
from tricycle_core.record_io import dump_toml
from tricycle_core.record_io import file_sha256 as digest
from tricycle_core.record_io import write_json as save

from tricycle_sampling.experiment import EXPERIMENT_SAMPLING_SCHEMA
from tricycle_sampling.protocols import LITERATURE_BASELINE_STRATEGIES, get_baseline_protocol
from tricycle_sampling.replay import core_hours as replay_core_hours
from tricycle_sampling.replay import cost_guidance as replay_cost_guidance
from tricycle_sampling.replay import mae as replay_mae
from tricycle_sampling.replay.campaign import (
    ROOT,
    check_controller,
    prepare,
    read,
    stage_round,
    supervise,
)
from tricycle_sampling.replay.report import write_text

VARIANTS = {
    'autonomous': {'name': 'autonomous-mae-v1', 'feedback': False, 'review': False},
    'feedback': {'name': 'autonomous-mae-v1', 'feedback': True, 'review': False},
    'reviewed': {'name': 'autonomous-mae-v1', 'feedback': True, 'review': True},
}
AGENT_VARIANTS = {
    **VARIANTS,
    'intelligent': {'name': replay_core_hours.NAME, 'feedback': True, 'review': True,
                   'guidance': replay_cost_guidance.NAME},
}


def prepare_suite(config_path: Path, directory: Path | None = None) -> Path:
    config = tomllib.loads(rio.read_text(config_path))
    schema = config.get('sampling_experiment')
    if schema is not None and schema != EXPERIMENT_SAMPLING_SCHEMA:
        raise ValueError('Unsupported sampling_experiment schema; prepare a new run')
    suite_id = config.pop('suite_id')
    requested = config.pop('sampling_strategies', None)
    if not isinstance(requested, list):
        raise ValueError('sampling_strategies must be supplied explicitly')
    variants = list(requested)
    if (not variants or len(variants) != len(set(variants))
            or set(variants) - set(AGENT_VARIANTS) - set(LITERATURE_BASELINE_STRATEGIES)):
        raise ValueError('sampling_strategies must be unique registered literature or agent strategies')
    config['sampling_experiment'] = EXPERIMENT_SAMPLING_SCHEMA
    template = replay_mae.settings(config)
    config.pop('decision_protocol', None)
    if config.get('acquisition_policy'):
        raise ValueError('MAE suite cannot use a controller cost penalty')
    root = rio.resolve(directory or timestamped_run_directory(ROOT / 'runs', suite_id))
    rio.mkdir(root, parents=True, exist_ok=False)
    execution_id = suite_id + '-' + hashlib.sha256(str(root).encode()).hexdigest()[:10]
    plan: dict[str, Any] = {
        'schema': 'tricycle.mae-suite/v1', 'suite_id': suite_id, 'execution_id': execution_id,
        'objective': replay_core_hours.objective(template), 'epochs': 300,
        'seeds_per_strategy': 5, 'rounds_per_seed': 40, 'maximum_sar_jobs': len(variants) * 200,
        'maximum_concurrent_sar_jobs': len(variants) * 5, 'pilot_evaluations': 3,
        'evaluation_role': 'evaluation-only; never supplied to agents', 'experiments': [],
    }
    if template and template['name'] == replay_core_hours.NAME:
        plan['budget_axis'] = 'core_hours'
        plan['evaluation_budgets_core_hours'] = template['evaluation_budgets_core_hours']
    config['training'] = {'epochs': 300}
    config['rollout'] = {'pilot_evaluations': 3}
    config['bohr']['shared_group_dir'] = str(root / 'shared-bohr')
    config['bohr']['job_group_name'] = execution_id
    for variant in variants:
        flags = AGENT_VARIANTS.get(variant, {'name': 'autonomous-mae-v1', 'feedback': False, 'review': False})
        protocol = {key: value for key, value in (template or {}).items() if key != 'guidance'}
        protocol.update(flags)
        if template:
            protocol['name'] = template['name']
        request = {**config, 'experiment_id': execution_id + '-' + variant,
                   'sampling_strategy': 'intelligent' if variant in AGENT_VARIANTS else variant,
                   'decision_protocol': protocol}
        if variant == 'intelligent':
            replay_mae.settings(request)
        path = root / 'configs' / f'{variant}.toml'
        rio.mkdir(path.parent, exist_ok=True)
        dump_toml(path, request)
        experiment = prepare(path, root / variant)
        frozen = read(experiment / 'experiment.json')
        seeds = sorted(rio.glob(experiment, 'seed-*'))
        if len(seeds) != 5 or frozen['round_count'] != 40 or frozen['batch_size'] != 40:
            raise ValueError('MAE suite requires exactly five matched 40-round trajectories')
        for seed in seeds:
            stage_round(experiment, seed, read(seed / 'state.json'), frozen)
        plan['experiments'].append({
            'variant': variant, 'root': str(experiment),
            **({'project': frozen['project']} if 'project' in frozen else {}),
            'config_sha256': digest(experiment / 'experiment.json'),
            'initial_pair_ids': [read(s / 'state.json')['selected_ids'] for s in seeds],
            'validation_panels': frozen['validation_panels'],
            'evaluation_sha256': frozen['evaluation_sha256'],
            'training': frozen['training'], 'source_revision': frozen['source_revision'],
            **({'sar_backend': frozen['sar_backend']} if 'sar_backend' in frozen else {}),
            **({'shared_protocol': {key: frozen[key] for key in (
                'sampling_experiment', 'fixed_seed', 'round_count', 'batch_size', 'private_hashes', 'assets_hashes')},
                'sampling_protocol': (get_baseline_protocol(variant).to_dict() if variant in LITERATURE_BASELINE_STRATEGIES
                                      else replay_cost_guidance.protocol() if variant == 'intelligent'
                                      else {'strategy': variant})}),
        })
        print(f'prepared {variant}', flush=True)
        save(root / 'suite.json', plan)
    reference = plan['experiments'][0]
    if 'sar_backend' in reference:
        plan['sar_backend'] = reference['sar_backend']
    for item in plan['experiments'][1:]:
        if item.get('sar_backend') != reference.get('sar_backend'):
            raise ValueError('suite mismatch: sar_backend')
        for key in ('initial_pair_ids', 'validation_panels', 'evaluation_sha256', 'training', 'source_revision', 'shared_protocol'):
            if item[key] != reference[key]:
                raise ValueError(f'suite mismatch: {key}')
    save(root / 'suite.json', plan)
    save(root / 'suite-hash.json', {'sha256': digest(root / 'suite.json')})
    save(root / 'preflight.json', {
        'status': 'prepared-not-submitted', 'strategy_count': len(variants), 'trajectory_count': len(variants) * 5,
        'first_round_bundles': len(variants) * 5, 'maximum_sar_jobs': len(variants) * 200,
        'matched_initial_sets': True, 'matched_fixed_auxiliary_panels': True,
        'matched_training_and_evaluation': True,
    })
    status(root)
    return root


def verified_plan(root: Path) -> dict[str, Any]:
    if digest(root / 'suite.json') != read(root / 'suite-hash.json')['sha256']:
        raise ValueError('frozen suite changed')
    plan = read(root / 'suite.json')
    for item in plan['experiments']:
        experiment = Path(item['root'])
        if digest(experiment / 'experiment.json') != item['config_sha256']:
            raise ValueError('frozen experiment changed')
        check_controller(experiment)
    return plan


def status(root: Path) -> dict[str, Any]:
    plan = verified_plan(root)
    states: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []
    for item in plan['experiments']:
        experiment = Path(item['root'])
        for seed in sorted(rio.glob(experiment, 'seed-*')):
            state = read(seed / 'state.json')
            points = [read(p) for p in sorted(rio.glob(seed, 'round-*/evaluation-summary.json'))]
            states.append({'variant': item['variant'], 'seed': state['seed'],
                           'phase': state['phase'], 'round': state['round'],
                           'sampled_pairs': len(state['selected_ids']),
                           'evaluations': len(points), 'external_id': state.get('external_id'),
                           'last_error': state.get('last_error'),
                           'waiting_for_pilot': state.get('waiting_for_pilot', False)})
            evaluations.extend({'variant': item['variant'], **point} for point in points)
    result = {'suite_id': plan['suite_id'], 'objective': plan['objective'], 'states': states,
              'complete': all(s['phase'] == 'complete' for s in states),
              'blocked': sum(s['phase'] == 'blocked' for s in states),
              'completed_evaluations': len(evaluations), 'maximum_evaluations': plan.get('maximum_sar_jobs', len(plan['experiments']) * 200)}
    save(root / 'status.json', result)
    save(root / 'evaluation-curves.json', {'evaluation_role': 'reporting-only', 'points': evaluations})
    if plan.get('budget_axis') == 'core_hours':
        report = replay_core_hours.aggregate(evaluations, [x['variant'] for x in plan['experiments']],
                                             plan['evaluation_budgets_core_hours'])
        save(root / 'evaluation-summary.json', {'evaluation_role': 'reporting-only', **report})
        lines = ['# Ground-truth MAE under core-hour budgets', '',
                 'Objective: minimize ground-truth MAE at predefined cumulative proxy core-hour budgets.',
                 'At each budget, use the latest affordable checkpoint per trajectory; report means only when all five matched seeds have observed coverage.',
                 'Budget milestones are for evaluation; unknown actual runtimes prevent a guaranteed hard cost cap. Record SAR and agent overhead separately.', '',
                 '| Strategy | Core-hour budget (core-hours) | Seeds | Mean MAE | Mean RMSE |',
                 '| --- | ---: | ---: | ---: | ---: |']
        for row in report['summaries']:
            lines.append(f"| {row['variant']} | {row['budget_core_hours']:g} | 5 | {row['mae_mean']:.4f} | {row['rmse_mean']:.4f} |")
        if report.get('intelligent_comparison'):
            comparison = report['intelligent_comparison']
            save(root / 'intelligent-comparison.json', comparison)
            lines += ['', 'Intelligent sampling objective: lower mean MAE than every static baseline across five matched seeds at every predefined core-hour milestone.',
                      f"Evaluation status: {comparison['status']}; insufficient coverage is pending; a tie or failure to outperform any baseline is not-met.", '',
                      '| Core-hour budget | Objective status | Methods missing complete coverage |', '| ---: | --- | --- |']
            for row in comparison['budgets']:
                lines.append(f"| {row['budget_core_hours']:g} | {row['status']} | {', '.join(row['missing_coverage'])} |")
            lines += ['', '[Paired comparisons of the intelligent strategy against all static baselines](intelligent-comparison.json); evaluation only, excluded from decisions.']
        lines += ['', '[Paired core-hour statistics](evaluation-summary.json) · [Frozen protocol](suite.json)', '',
                  '| Strategy | Seed | Phase | Evaluated rounds | Error |', '| --- | ---: | --- | ---: | --- |']
        for s in states:
            error = str(s['last_error'] or '').replace('|', '/').replace('\n', ' ')
            lines.append(f"| {s['variant']} | {s['seed']} | {s['phase']} | {s['evaluations']} | {error} |")
        write_text(root / 'REPORT.md', '\n'.join(lines))
        return result
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for point in evaluations:
        sampled = len(point['selected_ids'])
        grouped.setdefault((point['variant'], sampled), []).append(point)
    summaries = []
    for (variant, sampled), points in sorted(grouped.items()):
        values = [float(p['mae']) for p in points]
        summaries.append({'variant': variant, 'sampled_pairs': sampled, 'n_seeds': len(points),
                          'mae_mean': statistics.mean(values),
                          'mae_sd': statistics.stdev(values) if len(values) > 1 else None,
                          'rmse_mean': statistics.mean(float(p['rmse']) for p in points),
                          'complete_seed_support': len(points) == 5})
    paired = []
    for variant in ('feedback', 'reviewed'):
        for sampled in sorted({sampled for name, sampled in grouped if name == variant}):
            reference = {p['seed']: p for p in grouped.get(('autonomous', sampled), [])}
            by_seed = {p['seed']: p for p in grouped[(variant, sampled)]}
            common = sorted(reference.keys() & by_seed.keys())
            if common:
                deltas = [by_seed[s]['mae']-reference[s]['mae'] for s in common]
                paired.append({'variant': variant, 'reference': 'autonomous', 'sampled_pairs': sampled,
                               'matched_seeds': common, 'n_seeds': len(common),
                               'mae_delta_mean': statistics.mean(deltas),
                               'mae_delta_sd': statistics.stdev(deltas) if len(deltas) > 1 else None,
                               'complete_seed_support': len(common) == 5})
    save(root / 'evaluation-summary.json', {'evaluation_role': 'reporting-only', 'summaries': summaries,
                                           'paired_differences': paired,
                                           'limits': 'Partial seed support is descriptive; no significance claim. Initial 40-pair point precedes adaptive selection.'})
    lines = ['# MAE-first intelligent sampling experiment', '',
             f"{len(plan['experiments'])} strategies × five matched seeds × forty rounds; forty pairs per batch, with shared training configuration.",
             'Primary evaluation: main-model MAE at matched sample counts; report cost, RMSE, and runtime separately.',
             'The fixed auxiliary panel contains only eight initially labeled substrate pairs; its limited support does not establish main-model generalization error.', '',
             '| Strategy | Seed | Phase | Evaluated rounds | Current sampled pairs | Error |',
             '| --- | ---: | --- | ---: | ---: | --- |']
    for s in states:
        error = str(s['last_error'] or '').replace('|', '/').replace('\n', ' ')
        lines.append(f"| {s['variant']} | {s['seed']} | {s['phase']} | {s['evaluations']} | {s['sampled_pairs']} | {error} |")
    lines.extend(['', '## Main-model evaluation', '',
                  'Results for the initial 40 pairs precede intelligent selection. Means from fewer than five seeds describe progress only.', '',
                  '| Strategy | Sampled pairs | Seeds | Mean MAE | Mean RMSE |',
                  '| --- | ---: | ---: | ---: | ---: |'])
    for summary in summaries:
        if summary['sampled_pairs'] in (40, 400, 800, 1160, 1200, 1600):
            lines.append(f"| {summary['variant']} | {summary['sampled_pairs']} | {summary['n_seeds']} | {summary['mae_mean']:.4f} | {summary['rmse_mean']:.4f} |")
    lines.extend(['', '[Per-round evaluation data](evaluation-curves.json) · [Paired statistics](evaluation-summary.json) · [Frozen experiment manifest](suite.json)', ''])
    write_text(root / 'REPORT.md', '\n'.join(lines))
    return result


def watch(root: Path, *, live: bool, allow_submit: bool, once: bool) -> None:
    plan = verified_plan(root)
    if not live:
        status(root)
        return
    with rio.open(root / '.supervisor.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with ThreadPoolExecutor(max_workers=len(plan['experiments'])) as pool:
            futures = [pool.submit(supervise, Path(e['root']), live=live,
                                   allow_submit=allow_submit, once=once) for e in plan['experiments']]
            pending = set(futures)
            while pending:
                done, pending = wait(pending, timeout=30)
                status(root)
                for future in done:
                    future.result()
        status(root)


def service_file(root: Path) -> Path:
    plan = verified_plan(root)
    path = root / 'supervisor.plist'
    environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy',
                'https_proxy', 'all_proxy', 'no_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE',
                'TRICYCLE_TRAJECTORY_ALERTS_ENABLED', 'TRICYCLE_TRAJECTORY_ALERT_CONFIG'):
        if key in os.environ:
            environment[key] = os.environ[key]
    with rio.open(path, 'wb') as handle:
        plistlib.dump({'Label': 'org.tricycle.' + plan['execution_id'],
                      'ProgramArguments': [sys.executable, '-m', 'tricycle_sampling.replay.suite',
                                           'watch', '--run-dir', str(root), '--live', '--allow-submit'],
                      'WorkingDirectory': str(ROOT), 'RunAtLoad': True,
                      'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 60,
                      'StandardOutPath': str(root / 'supervisor.log'),
                      'StandardErrorPath': str(root / 'supervisor.stderr.log'),
                      'EnvironmentVariables': environment}, handle)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'status', 'tick', 'watch', 'service-file'))
    parser.add_argument('--config', type=Path)
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--allow-submit', action='store_true')
    args = parser.parse_args()
    if args.action == 'prepare':
        if args.config is None:
            parser.error('prepare requires --config')
        print(prepare_suite(args.config, args.run_dir))
        return
    if args.run_dir is None:
        parser.error('--run-dir is required')
    root = rio.resolve(args.run_dir)
    if args.action == 'service-file':
        if not args.allow_submit:
            parser.error('service-file requires explicit --allow-submit')
        print(service_file(root))
    elif args.action == 'status':
        print(status(root))
    else:
        watch(root, live=args.live, allow_submit=args.allow_submit, once=args.action == 'tick')


if __name__ == '__main__':
    main()
