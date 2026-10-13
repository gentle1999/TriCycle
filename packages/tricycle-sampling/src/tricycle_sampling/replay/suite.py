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

from tricycle_core import timestamped_run_directory
from tricycle_core.io import file_sha256 as digest
from tricycle_core.io import write_json as save
from tricycle_core.toml import dump_toml

from tricycle_sampling.experiment import EXPERIMENT_SAMPLING_SCHEMA
from tricycle_sampling.protocols import LITERATURE_BASELINE_STRATEGIES, get_baseline_protocol
from tricycle_sampling.replay import core_hours as replay_core_hours
from tricycle_sampling.replay import cost_guidance as replay_cost_guidance
from tricycle_sampling.replay import mae as replay_mae
from tricycle_sampling.replay.campaign import (
    PACKAGE_ROOT,
    ROOT,
    check_hashes,
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
    'cost-aware': {'name': replay_core_hours.NAME, 'feedback': True, 'review': True,
                   'guidance': replay_cost_guidance.NAME},
}


def prepare_suite(config_path: Path, directory: Path | None = None) -> Path:
    config = tomllib.loads(config_path.read_text())
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
    root = (directory or timestamped_run_directory(ROOT / 'runs', suite_id)).resolve()
    root.mkdir(parents=True, exist_ok=False)
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
        if variant == 'cost-aware':
            replay_mae.settings(request)
        path = root / 'configs' / f'{variant}.toml'
        path.parent.mkdir(exist_ok=True)
        dump_toml(path, request)
        experiment = prepare(path, root / variant)
        frozen = read(experiment / 'experiment.json')
        seeds = sorted(experiment.glob('seed-*'))
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
                                      else replay_cost_guidance.protocol() if variant == 'cost-aware'
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
        check_hashes(PACKAGE_ROOT, read(experiment / 'experiment.json')['controller_hashes'])
    return plan


def status(root: Path) -> dict[str, Any]:
    plan = verified_plan(root)
    states: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []
    for item in plan['experiments']:
        experiment = Path(item['root'])
        for seed in sorted(experiment.glob('seed-*')):
            state = read(seed / 'state.json')
            points = [read(p) for p in sorted(seed.glob('round-*/evaluation-summary.json'))]
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
        lines = ['# 核时预算下的 Ground-truth MAE', '',
                 '目标：在预设累计代理计算核时预算下最小化 ground-truth MAE。',
                 '各预算取每条轨迹最新的预算内 checkpoint；五个匹配 seed 均有观测覆盖后才报告均值。',
                 '预算节点用于评价，未知真实耗时无法保证硬费用上限。SAR/智能体开销另记。', '',
                 '| 策略 | 核时预算（core-hours） | seed数 | MAE均值 | RMSE均值 |',
                 '| --- | ---: | ---: | ---: | ---: |']
        for row in report['summaries']:
            lines.append(f"| {row['variant']} | {row['budget_core_hours']:g} | 5 | {row['mae_mean']:.4f} | {row['rmse_mean']:.4f} |")
        if report.get('intelligent_comparison'):
            comparison = report['intelligent_comparison']
            save(root / 'intelligent-comparison.json', comparison)
            lines += ['', '智能采样策略的目标：在全部预设核时节点，匹配五个 seed 的平均 MAE 低于每条静态基线。',
                      f"评价状态：{comparison['status']}；覆盖不足为 pending，持平或未优于任一基线为 not-met。", '',
                      '| 核时预算 | 达标状态 | 缺少完整覆盖的方法 |', '| ---: | --- | --- |']
            for row in comparison['budgets']:
                lines.append(f"| {row['budget_core_hours']:g} | {row['status']} | {', '.join(row['missing_coverage'])} |")
            lines += ['', '[智能策略与全部静态基线的配对比较](intelligent-comparison.json)；仅用于评价，不进入决策。']
        lines += ['', '[核时配对统计](evaluation-summary.json) · [冻结协议](suite.json)', '',
                  '| 策略 | seed | 阶段 | 已评价轮数 | 错误 |', '| --- | ---: | --- | ---: | --- |']
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
    lines = ['# MAE 优先智能采样实验', '',
             f"{len(plan['experiments'])} 种策略 × 五个匹配 seed × 四十轮；每批四十对，统一训练配置。",
             '主要评价：匹配样本量下的主模型 MAE；成本、RMSE 和运行耗时单独报告。',
             '固定辅助验证仅含八个初始有标签底物对，支持有限，不代表主模型泛化误差。', '',
             '| 策略 | seed | 阶段 | 已评价轮数 | 当前采样对数 | 错误 |',
             '| --- | ---: | --- | ---: | ---: | --- |']
    for s in states:
        error = str(s['last_error'] or '').replace('|', '/').replace('\n', ' ')
        lines.append(f"| {s['variant']} | {s['seed']} | {s['phase']} | {s['evaluations']} | {s['sampled_pairs']} | {error} |")
    lines.extend(['', '## 主模型评价', '',
                  '初始40对的结果先于智能选样。未齐五个seed的均值仅作进度描述。', '',
                  '| 策略 | 样本对数 | seed数 | MAE均值 | RMSE均值 |',
                  '| --- | ---: | ---: | ---: | ---: |'])
    for summary in summaries:
        if summary['sampled_pairs'] in (40, 400, 800, 1160, 1200, 1600):
            lines.append(f"| {summary['variant']} | {summary['sampled_pairs']} | {summary['n_seeds']} | {summary['mae_mean']:.4f} | {summary['rmse_mean']:.4f} |")
    lines.extend(['', '[逐轮评价数据](evaluation-curves.json) · [配对统计](evaluation-summary.json) · [冻结实验清单](suite.json)', ''])
    write_text(root / 'REPORT.md', '\n'.join(lines))
    return result


def watch(root: Path, *, live: bool, allow_submit: bool, once: bool) -> None:
    plan = verified_plan(root)
    if not live:
        status(root)
        return
    with (root / '.supervisor.lock').open('a') as lock:
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
                'https_proxy', 'all_proxy', 'no_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE'):
        if key in os.environ:
            environment[key] = os.environ[key]
    with path.open('wb') as handle:
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
    root = args.run_dir.resolve()
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
