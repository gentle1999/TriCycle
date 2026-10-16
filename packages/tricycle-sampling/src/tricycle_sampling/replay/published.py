"""Portable replay observations with independently recomputable costs and errors."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
from tricycle_core import record_io as rio
from tricycle_core.metrics import regression_metrics
from tricycle_core.record_io import file_sha256, write_json
from tricycle_sar.predictions import validate_rows

from .record_store import scientific_view


def read(path: Path) -> dict:
    return json.loads(rio.read_text(path))


def rows(path: Path) -> list[dict]:
    with rio.open(path, newline='') as handle:
        return list(csv.DictReader(handle))


def table(path: Path, values: list[dict]) -> None:
    rio.mkdir(path.parent, parents=True, exist_ok=True)
    with rio.open(path, 'w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(values[0]))
        writer.writeheader()
        writer.writerows(values)


def asset_file(reference: dict, name: str) -> Path:
    """Verify one scientific asset and its manifest, without loading checkpoints."""
    if reference['schema'] not in {'tricycle.asset-tree/v1', 'tricycle.asset-tree/v2'}:
        raise ValueError('Unsupported asset reference')
    digest = reference['sha256']
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('Invalid asset digest')
    store = Path(reference['store'])
    manifest = read(store / 'manifests' / f'{digest}.json')
    if hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest() != digest:
        raise ValueError('Asset manifest changed')
    files = manifest['files'] if reference['schema'].endswith('/v2') else manifest
    root = store / 'trees' / digest
    path = root / name
    if not rio.resolve(path).is_relative_to(rio.resolve(root)) or rio.is_symlink(path):
        raise ValueError('Unsafe scientific asset')
    entry = files[name]
    if path.stat().st_size != entry['bytes'] or file_sha256(path) != entry['sha256']:
        raise ValueError(f'Scientific asset changed: {name}')
    return path


def checksum_inventory(root: Path) -> None:
    files = sorted(p for p in rio.rglob(root, '*') if rio.is_file(p) and p.name != 'SHA256SUMS')
    rio.write_text(root / 'SHA256SUMS', ''.join(f'{file_sha256(p)}  {p.relative_to(root).as_posix()}\n' for p in files))


def export(plan: Path, output: Path) -> dict:
    """Read an explicit inventory of restored evidence; never advance a trajectory."""
    if rio.exists(output):
        raise FileExistsError(output)
    specification = read(plan)
    rio.mkdir(output, parents=True)
    rio.mkdir(output / 'data')
    points, coverage, selections, sources, protocols, matrices = [], [], [], {}, [], []
    ground_truth = None
    worker_targets = None
    scope = None
    pools = {}
    with rio.open(output / 'decision-records.jsonl.gz', 'wb') as raw_decisions, \
            gzip.GzipFile(filename='', fileobj=raw_decisions, mode='wb', mtime=0) as compressed_decisions, \
            io.TextIOWrapper(compressed_decisions, encoding='utf-8') as decisions:
        for cohort in specification['cohorts']:
            root = Path(cohort['root'])
            original = Path(cohort['original_root'])
            plan_path = root / 'sampling-plan.json'
            sampling_plan = read(plan_path)
            sources[str(original / 'sampling-plan.json')] = file_sha256(plan_path)
            for item in sampling_plan['experiments']:
                relative = Path(item['root']).relative_to(original)
                experiment = root / relative
                config_path = experiment / 'experiment.json'
                config = read(config_path)
                config_hash = file_sha256(config_path)
                if config_hash != read(experiment / 'experiment-hash.json')['sha256']:
                    raise ValueError('Experiment binding changed')
                suite = experiment.parent
                suite_hash = file_sha256(suite / 'suite.json')
                if suite_hash != read(suite / 'suite-hash.json')['sha256']:
                    raise ValueError('Suite binding changed')
                bound = next(e for e in read(suite / 'suite.json')['experiments'] if e['root'] == item['root'])
                if bound['config_sha256'] != config_hash:
                    raise ValueError('Suite/experiment binding differs')
                space, variant = item['project'], item['variant']
                protocol_name = f"protocols/{cohort['name']}/{space}/{variant}.json"
                target = output / protocol_name
                rio.mkdir(target.parent, parents=True, exist_ok=True)
                rio.copyfile(config_path, target)
                protocols.append({'cohort': cohort['name'], 'space': space, 'variant': variant,
                                  'path': protocol_name, 'sha256': config_hash,
                                  'original_path': str(original / relative / 'experiment.json')})
                sources[str(original / relative / 'experiment.json')] = config_hash
                sources[str(original / relative.parent / 'suite.json')] = suite_hash
                gt = asset_file(config['asset_references']['private'], 'evaluation.csv')
                pool = asset_file(config['asset_references']['private'], 'pair-pool.csv')
                for source, name in [(gt, 'evaluation.csv'), (pool, 'pair-pool.csv')]:
                    if file_sha256(source) != config['private_hashes'][name]:
                        raise ValueError('Frozen private input changed')
                    sources[str(source)] = file_sha256(source)
                if ground_truth is None:
                    rio.copyfile(gt, output / 'data/evaluation.csv')
                    ground_truth = rows(gt)
                elif file_sha256(gt) != file_sha256(output / 'data/evaluation.csv'):
                    raise ValueError('Different GT datasets')
                if space not in pools:
                    pool_output = output / f'data/pools/{space}.csv'
                    rio.mkdir(pool_output.parent, parents=True, exist_ok=True)
                    rio.copyfile(pool, pool_output)
                    pools[space] = {r['pair_id']: r for r in rows(pool)}
                    for name, group in [('project-labels.csv', 'labels')]:
                        source = asset_file(config['asset_references']['private'], name)
                        if file_sha256(source) != config['private_hashes'][name]:
                            raise ValueError('Frozen labels changed')
                        dest = output / f'data/{group}/{space}.csv'
                        rio.mkdir(dest.parent, parents=True, exist_ok=True)
                        rio.copyfile(source, dest)
                        sources[str(source)] = file_sha256(source)
                    for name in ('prior-structures.csv', 'route-id-vocabulary.json'):
                        source = asset_file(config['asset_references']['assets'], name)
                        if file_sha256(source) != config['assets_hashes'][name]:
                            raise ValueError('Frozen prior changed')
                        dest = output / 'data/prior' / space / name
                        rio.mkdir(dest.parent, parents=True, exist_ok=True)
                        rio.copyfile(source, dest)
                        sources[str(source)] = file_sha256(source)
                elif file_sha256(pool) != file_sha256(output / f'data/pools/{space}.csv'):
                    raise ValueError('Cohorts use different cost pools')
                count = read(suite / 'suite.json')['seeds_per_strategy']
                states = sorted(rio.glob(experiment, 'seed-*/state.json'))
                if [read(p)['seed'] for p in states] != list(range(count)):
                    raise ValueError('Missing seed states')
                for state_path in states:
                    state = read(state_path)
                    sources[str(original / state_path.relative_to(root))] = file_sha256(state_path)
                    with scientific_view(state_path.parent) as record_root:
                        paths = sorted(rio.glob(record_root, 'round-*/evaluation-summary.json'))
                        previous: set[str] = set()
                        for index, path in enumerate(paths):
                            summary = read(path)
                            if summary['round_index'] != index or summary['seed'] != state['seed']:
                                raise ValueError('Noncontiguous trajectory or wrong seed')
                            result_ref = read(path.parent / 'result-asset-reference.json')
                            result_paths = {name: asset_file(result_ref, name) for name in
                                            ('worker-report.json', 'evaluation.json', 'evaluation-predictions.csv')}
                            report, evaluation = read(result_paths['worker-report.json']), read(result_paths['evaluation.json'])
                            for name in ('evaluation.json', 'evaluation-predictions.csv'):
                                if file_sha256(result_paths[name]) != report['outputs'][name]:
                                    raise ValueError('Worker result hash mismatch')
                            identity = [evaluation['ground_truth_sha256'], evaluation['n'], evaluation['unit'],
                                        report['source_revision'], config['training']['epochs']]
                            if scope is None:
                                scope = identity
                            if (scope != identity or identity[0] != config['evaluation_sha256']
                                    or identity[3] != config['source_revision'] or evaluation['role'] != 'evaluation-only'):
                                raise ValueError('Evaluation/source scope differs')
                            if any(summary[key] != evaluation[key] for key in ('mae', 'rmse', 'n', 'unit', 'role', 'ground_truth_sha256')):
                                raise ValueError('Summary differs from worker result')
                            predictions = rows(result_paths['evaluation-predictions.csv'])
                            validate_rows(predictions, expected=ground_truth, required=('prediction', 'target'))
                            indexed = {r['path_id']: r for r in predictions}
                            ordered = [indexed[r['path_id']] for r in ground_truth]
                            targets = np.array([float(r['target']) for r in ordered], dtype=np.float64)
                            if worker_targets is None:
                                worker_targets = targets
                            if not np.array_equal(worker_targets, targets):
                                raise ValueError('Worker target representations differ')
                            prediction = np.array([float(r['prediction']) for r in ordered], dtype=np.float64)
                            measured = regression_metrics(targets.tolist(), prediction.tolist())
                            for key in ('mae', 'rmse', 'bias', 'n'):
                                if not math.isclose(measured[key], evaluation[key], rel_tol=0, abs_tol=2e-12):
                                    raise ValueError('Recorded evaluation cannot be reproduced')
                            selected = summary['selected_ids']
                            if len(selected) != len(set(selected)) or not previous <= set(selected):
                                raise ValueError('Noncumulative or duplicate acquisitions')
                            cost = math.fsum(float(pools[space][p]['materialized_sampling_cost_core_hours']) for p in selected)
                            if not math.isclose(cost, summary['cumulative_sampling_cost_core_hours'], rel_tol=0, abs_tol=1e-6):
                                raise ValueError('Sampling cost mismatch')
                            point = {'cohort': cohort['name'], 'space': space, 'variant': variant, 'seed': state['seed'],
                                     'round_index': index, 'cost': cost, 'mae': evaluation['mae'], 'rmse': evaluation['rmse'],
                                     'bias': evaluation['bias'], 'n_gt': evaluation['n'], 'sampled_pairs': len(selected)}
                            points.append(point)
                            selections.append({'new_ids': [p for p in selected if p not in previous]})
                            previous = set(selected)
                            matrices.append(prediction)
                            for source in (path, path.parent / 'result-asset-reference.json'):
                                sources[str(original / state_path.parent.relative_to(root) / source.relative_to(record_root))] = file_sha256(source)
                            for source in result_paths.values():
                                sources[str(source)] = file_sha256(source)
                            decision = {}
                            for name in ('task.json', 'decision-input.json', 'proposal-response.json', 'review-response.json',
                                         'selection.json', 'ACQUISITION.md', 'acquisition-summary.json', 'response.json'):
                                source = path.parent / 'decision' / name
                                if rio.exists(source):
                                    decision[name] = rio.read_text(source)
                                    sources[str(original / state_path.parent.relative_to(root) / source.relative_to(record_root))] = file_sha256(source)
                            if decision:
                                decisions.write(json.dumps({'checkpoint_index': len(points) - 1, 'records': decision}, ensure_ascii=False) + '\n')
                        coverage.append({'cohort': cohort['name'], 'space': space, 'variant': variant,
                                         'seed': state['seed'], 'phase': state['phase'], 'completed_checkpoints': len(paths),
                                         'expected_checkpoints': config['round_count'], 'last_error': state.get('last_error', '')})
                print(f"Exported {cohort['name']} {space}/{variant}", flush=True)
    table(output / 'data/checkpoints.csv', points)
    table(output / 'data/coverage.csv', coverage)
    rio.write_text(output / 'data/selections.jsonl', ''.join(json.dumps(r) + '\n' for r in selections))
    if worker_targets is None or not matrices:
        raise ValueError('No completed scientific observations')
    np.savez_compressed(output / 'data/evaluation-predictions.npz', predictions=np.stack(matrices), targets=worker_targets)
    write_json(output / 'source-index.json', sources)
    write_json(output / 'manifest.json', {'schema': 'tricycle.replay.published/v1', 'evaluation_scope': scope,
               'protocols': protocols, 'checkpoints': len(points), 'trajectories': len(coverage),
               'prediction_layout': 'Rows follow checkpoints.csv; columns follow evaluation.csv; float64, lossless compression.',
               'metric_target': 'Preserve worker-exported targets (float32-rounded GT) to reproduce recorded MAE exactly.',
               'cost': 'Sum unique acquired pair TS core-hours, including initialization; exclude model and agent overhead.',
               'source_plan_sha256': file_sha256(plan)})
    checksum_inventory(output)
    return verify(output)


def verify(root: Path) -> dict:
    """Recompute every published metric and cost without original runs or assets."""
    indexed = set()
    for line in rio.read_text(root / 'SHA256SUMS').splitlines():
        digest, name = line.split('  ', 1)
        path = root / name
        if name in indexed or not rio.resolve(path).is_relative_to(rio.resolve(root)) or rio.is_symlink(path):
            raise ValueError('Unsafe or duplicate checksum entry')
        if file_sha256(path) != digest:
            raise ValueError(f'Report checksum differs: {name}')
        indexed.add(name)
    actual = {p.relative_to(root).as_posix() for p in rio.rglob(root, '*') if rio.is_file(p) and p.name != 'SHA256SUMS'}
    if indexed != actual:
        raise ValueError('Report inventory differs')
    manifest = read(root / 'manifest.json')
    if manifest['schema'] != 'tricycle.replay.published/v1':
        raise ValueError('Unsupported report schema')
    gt = rows(root / 'data/evaluation.csv')
    if file_sha256(root / 'data/evaluation.csv') != manifest['evaluation_scope'][0]:
        raise ValueError('GT hash mismatch')
    points = rows(root / 'data/checkpoints.csv')
    coverage = rows(root / 'data/coverage.csv')
    selections = [json.loads(line) for line in rio.read_text(root / 'data/selections.jsonl').splitlines()]
    with np.load(root / 'data/evaluation-predictions.npz', allow_pickle=False) as arrays:
        predictions, targets = arrays['predictions'], arrays['targets']
    if predictions.shape != (len(points), len(gt)) or targets.shape != (len(gt),) or len(points) != len(selections):
        raise ValueError('Prediction/selection dimensions differ')
    truth = np.array([float(r['G_T_activate']) for r in gt])
    if not np.array_equal(targets, truth.astype(np.float32).astype(np.float64)) and not np.array_equal(targets, truth):
        raise ValueError('Reported targets differ from GT or its float32 representation')
    pools = {p.stem: {r['pair_id']: float(r['materialized_sampling_cost_core_hours']) for r in rows(p)}
             for p in rio.glob(root / 'data/pools', '*.csv')}
    history: dict[tuple, set[str]] = defaultdict(set)
    counts: dict[tuple, int] = defaultdict(int)
    first = {}
    max_frozen_delta = 0.0
    for index, point in enumerate(points):
        key = point['cohort'], point['space'], point['variant'], int(point['seed'])
        if int(point['round_index']) != counts[key]:
            raise ValueError('Noncontiguous report trajectory')
        new = selections[index]['new_ids']
        if len(new) != len(set(new)) or history[key].intersection(new):
            raise ValueError('Repeated acquisition')
        if counts[key] == 0:
            first[key] = set(new)
        history[key].update(new)
        counts[key] += 1
        cost = math.fsum(pools[point['space']][p] for p in history[key])
        if len(history[key]) != int(point['sampled_pairs']) or not math.isclose(cost, float(point['cost']), rel_tol=0, abs_tol=1e-6):
            raise ValueError('Published sampling cost differs')
        metric = regression_metrics(targets.tolist(), predictions[index].tolist())
        if int(point['n_gt']) != len(gt):
            raise ValueError('Evaluation coverage differs')
        for name in ('mae', 'rmse', 'bias'):
            if not math.isclose(metric[name], float(point[name]), rel_tol=0, abs_tol=2e-12):
                raise ValueError('Published metric differs')
        max_frozen_delta = max(max_frozen_delta, abs(float(np.mean(np.abs(predictions[index] - truth))) - metric['mae']))
    for key, selected in first.items():
        if key[0] == 'english' and selected != first['chinese', *key[1:]]:
            raise ValueError('Language cohorts use different initial selections')
    if len(coverage) != len(counts) or len(coverage) != manifest['trajectories'] or len(points) != manifest['checkpoints']:
        raise ValueError('Published trajectory coverage differs')
    for state in coverage:
        key = state['cohort'], state['space'], state['variant'], int(state['seed'])
        if counts[key] != int(state['completed_checkpoints']):
            raise ValueError('Coverage table differs')
        if state['phase'] == 'complete' and counts[key] != int(state['expected_checkpoints']):
            raise ValueError('Incomplete trajectory claimed complete')
    for entry in manifest['protocols']:
        config = read(root / entry['path'])
        if file_sha256(root / entry['path']) != entry['sha256']:
            raise ValueError('Protocol hash differs')
        if file_sha256(root / f"data/pools/{entry['space']}.csv") != config['private_hashes']['pair-pool.csv']:
            raise ValueError('Pool binding differs')
        if file_sha256(root / f"data/labels/{entry['space']}.csv") != config['private_hashes']['project-labels.csv']:
            raise ValueError('Training label binding differs')
        for name in ('prior-structures.csv', 'route-id-vocabulary.json'):
            if file_sha256(root / 'data/prior' / entry['space'] / name) != config['assets_hashes'][name]:
                raise ValueError('Prior binding differs')
    return {'status': 'verified', 'checkpoints': len(points), 'trajectories': len(coverage),
            'prediction_values': int(predictions.size), 'gt_paths': len(gt),
            'maximum_mae_change_using_unrounded_gt': max_frozen_delta,
            'scope': 'Scientific observations and costs; no checkpoint loading or model execution'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('export', 'verify'))
    parser.add_argument('--result-dir', type=Path, required=True)
    parser.add_argument('--plan', type=Path)
    args = parser.parse_args()
    if args.action == 'export' and args.plan is None:
        parser.error('export requires --plan')
    print(json.dumps(export(args.plan, args.result_dir) if args.action == 'export' else verify(args.result_dir), indent=2))


if __name__ == '__main__':
    main()
