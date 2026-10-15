"""Audit executed intelligent-sampling decisions and realized route features offline."""
from __future__ import annotations

import argparse
import json
import statistics as stats
from collections import Counter
from pathlib import Path

from tricycle_sampling.acquisition import evaluate_numeric_expression

from .replay_sampling_panels import SPACES, read, sha, write_csv
from .threshold_cost import compare

FEATURES = ('sampled_novelty', 'pool_local_density', 'estimated_core_hours',
            'product_ring_count', 'mean_product_atom_count', 'product_count',
            'epistemic_variance_per_product')


def ranked_ids(candidates: list[dict], expression: str, cost_expression: str,
               context: dict, batch_size: int) -> set[str]:
    scored = []
    for c in candidates:
        payload = c['payload']
        cost = evaluate_numeric_expression(cost_expression, candidate=payload, context=context)
        score = evaluate_numeric_expression(expression, candidate=payload, context=context,
                                            estimated_cost=cost)
        scored.append((-score, c['candidate_id']))
    return {cid for _, cid in sorted(scored)[:batch_size]}


def audit(snapshot: dict) -> tuple[list[dict], dict[str, str]]:
    root = Path(snapshot['run_dir'])
    points = {(p['space'], p['variant'], p['seed'], p['round_index']): p for p in snapshot['points']}
    rows: list[dict] = []
    sources: dict[str, str] = {}

    def evidence(path: Path) -> dict:
        sources[str(path)] = sha(path)
        return read(path)

    for space in SPACES:
        for seed in range(5):
            track = root/'suites'/space/'intelligent'/f'seed-{seed:02d}'
            previous: dict | None = None
            for file in sorted(track.glob('round-*/decision/acquisition-summary.json')):
                s = evidence(file)
                target = int(file.parents[1].name.removeprefix('round-'))+1
                key = (space, 'intelligent', seed, target)
                if key not in points:
                    continue  # Snapshot-completed outcomes only, not pending proposals.
                directory = file.parent
                selection = evidence(directory/'selection.json')
                ids = set(selection['candidate_ids'])
                outcome = evidence(Path(points[key]['evaluation_file']))
                if ids != set(outcome['new_ids']):
                    raise ValueError(f'Decision/outcome identity mismatch: {file}')
                if selection['acquisition_function_hash'] != s['function_hash']:
                    raise ValueError(f'Acquisition hash mismatch: {file}')
                if s['decision_source'] == 'controller-all-remaining':
                    continue
                response = evidence(directory/'response.json')
                receipt = evidence(directory/'accepted-response.json')
                if sha(directory/'response.json') != receipt['sha256'] or receipt['sha256'] != s['response_sha256']:
                    raise ValueError(f'Unaccepted response: {file}')
                if response['expression'] != s['expression']:
                    raise ValueError(f'Formula summary mismatch: {file}')
                hashes = evidence(directory/'evidence-hashes.json')
                candidates = evidence(directory/'pair-candidates.json')['candidates']
                task = evidence(directory/'task.json')
                for name in ('pair-candidates.json', 'task.json'):
                    if hashes[name] != sha(directory/name):
                        raise ValueError(f'Input hash mismatch: {directory/name}')
                selected = [c for c in candidates if c['candidate_id'] in ids]
                if len(selected) != len(ids):
                    raise ValueError(f'Missing selected candidate: {file}')
                context = task['context']
                own_ids = ranked_ids(candidates, s['expression'], s['cost_expression'], context, len(ids))
                if own_ids != ids:
                    raise ValueError(f'Formula replay differs from selection: {file}')
                overlap: float | None = None
                if previous is not None:
                    old = ranked_ids(candidates, previous['expression'], previous['cost_expression'],
                                     context, len(ids))
                    overlap = len(old & ids)/len(ids)
                before = points[(space, 'intelligent', seed, target-1)]
                row: dict = {'space': space, 'seed': seed, 'selected_round': target,
                       'stage': 'early:1-9' if target < 10 else 'middle:10-24' if target < 25 else 'late:25-38',
                       'action': s['decision_action'], 'source': s['decision_source'],
                       'expression': s['expression'], 'rationale': s['rationale'],
                       'has_uncertainty': 'variance' in s['expression'],
                       'has_ring': 'ring_count' in s['expression'],
                       'has_region': 'validation_region' in s['expression'],
                       'previous_rule_overlap': overlap,
                       'new_pairs': len(ids), 'sampled_pairs': points[key]['sampled_pairs'],
                       'batch_cost': points[key]['cost']-before['cost'],
                       'cumulative_cost': points[key]['cost'], 'gt_mae': points[key]['mae'],
                       'gt_mae_change': points[key]['mae']-before['mae'],
                       'selected_high_ring_fraction': stats.mean(c['payload']['product_ring_count'] > 2 for c in selected),
                       'pool_high_ring_fraction': stats.mean(c['payload']['product_ring_count'] > 2 for c in candidates),
                       'unique_ene': len({cid.split('+')[0] for cid in ids}),
                       'unique_diene': len({cid.split('+')[1] for cid in ids}),
                       'decision_file': str(file)}
                for feature in FEATURES:
                    row['selected_'+feature] = stats.mean(c['payload'][feature] for c in selected)
                    row['pool_'+feature] = stats.mean(c['payload'][feature] for c in candidates)
                row['estimated_cost_ratio'] = row['selected_estimated_core_hours']/row['pool_estimated_core_hours']
                row['novelty_delta'] = row['selected_sampled_novelty']-row['pool_sampled_novelty']
                row['sparsity_delta'] = row['pool_pool_local_density']-row['selected_pool_local_density']
                row['realized_to_estimated_cost'] = row['batch_cost']/(len(ids)*row['selected_estimated_core_hours'])
                rows.append(row)
                previous = s
    return rows, sources


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args()
    output = args.out_dir
    output.mkdir(parents=True, exist_ok=False)
    snapshot = read(args.snapshot)
    rows, sources = audit(snapshot)
    write_csv(output/'executed-decisions.csv', rows)
    summaries, stages = [], []
    for space in SPACES:
        group = [r for r in rows if r['space'] == space]
        adjustments = [r for r in group if r['action']=='adjust' and r['previous_rule_overlap'] is not None]
        summaries.append({'space': space, 'decisions': len(group), 'actions': dict(Counter(r['action'] for r in group)),
                          'uncertainty_rounds': sum(r['has_uncertainty'] for r in group),
                          'ring_rounds': sum(r['has_ring'] for r in group),
                          'region_rounds': sum(r['has_region'] for r in group),
                          'adjustments_with_same_batch': sum(r['previous_rule_overlap']==1 for r in adjustments),
                          'adjustment_median_replaced_candidates': stats.median(40*(1-r['previous_rule_overlap']) for r in adjustments),
                          'median_realized_to_estimated_cost': stats.median(r['realized_to_estimated_cost'] for r in group)})
        for stage in ('early:1-9', 'middle:10-24', 'late:25-38'):
            local = [r for r in group if r['stage'] == stage]
            record: dict = {'space': space, 'stage': stage, 'decisions': len(local), 'seeds': len({r['seed'] for r in local})}
            for field in ('estimated_cost_ratio', 'novelty_delta', 'sparsity_delta',
                          'selected_high_ring_fraction', 'pool_high_ring_fraction', 'selected_mean_product_atom_count',
                          'selected_product_count', 'selected_sampled_novelty', 'unique_ene', 'unique_diene',
                          'batch_cost', 'realized_to_estimated_cost'):
                # Equal weight for seeds with observations, avoiding completed-track over-weighting.
                by_seed = [stats.mean(r[field] for r in local if r['seed']==seed)
                           for seed in sorted({r['seed'] for r in local})]
                record[field] = stats.mean(by_seed)
            stages.append(record)
    _, costs, comparisons = compare(snapshot['points'], [5., 4., 3., 2., 1.])
    write_csv(output/'route-stages.csv', stages)
    write_csv(output/'threshold-costs.csv', costs)
    report = {'snapshot_at': snapshot['snapshot_at'], 'snapshot_sha256': sha(args.snapshot),
              'summaries': summaries, 'stages': stages, 'threshold_comparisons': comparisons,
              'states': [s for s in snapshot['statuses'] if s.get('variant')=='intelligent'],
              'scope': 'Executed agent decisions with snapshot-completed next-round outcomes. Initialization and controller-all-remaining excluded. '
                       'Replayed final accepted formula and previous formula on current pool. Stages use equal seed weights among observed seeds; '
                       'late incomplete tracks remain partial. Formula changes and selected-label errors do not establish causal GT gains.',
              'script_sha256': sha(Path(__file__)), 'sources': sources}
    (output/'audit.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'snapshot_at': report['snapshot_at'], 'summaries': summaries, 'stages': stages}, indent=2))


if __name__ == '__main__':
    main()
