"""Independently replay committed MAE decisions without changing run artifacts.

The TOML artifact stores contexts as JSON strings. Its complete hashed
definition, including MAE decision fields, is reconstructed from the frozen
request and written as a separate audit artifact; original files stay intact.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import tomllib
from pathlib import Path
from typing import Any

from tricycle_core.io import file_sha256 as digest
from tricycle_core.io import write_json as save

from tricycle_sampling import AgentAuthoredAcquisitionSamplingStrategy, Candidate
from tricycle_sampling.replay.campaign import check_hashes
from tricycle_sampling.replay.suite import verified_plan


def audit_decision(selection_path: Path) -> dict[str, Any]:
    directory = selection_path.parent
    selection = json.loads(selection_path.read_text())
    task_path = directory / 'task.json'
    task = json.loads(task_path.read_text())
    response_path = directory / 'response.json'
    response = json.loads(response_path.read_text())
    request_path = directory / 'selection-request.toml'
    request = tomllib.loads(request_path.read_text())
    if response['task_sha256'] != digest(task_path):
        raise ValueError('response task binding changed')
    if selection['request_sha256'] != digest(request_path):
        raise ValueError('selection request hash changed')
    candidates = json.loads((directory / 'pair-candidates.json').read_text())['candidates']
    ids = {c['candidate_id'] for c in candidates}
    history = json.loads((directory / 'acquisition-history.json').read_text())
    if len(ids) != len(candidates) or ids & set(history['selected_ids']):
        raise ValueError('candidate pool duplicates or overlaps prior selections')
    if len(selection['candidate_ids']) != task['budget'] or len(set(selection['candidate_ids'])) != task['budget']:
        raise ValueError('selected candidate budget or uniqueness changed')
    round_dir = Path(request['output_dir']) / 'rounds' / f"{task['round']:04d}"
    artifact = tomllib.loads((round_dir / 'acquisition-function.toml').read_text())
    spec = {**request['metadata']['agent_acquisition'],
            'context': json.loads(artifact['context_json'])}
    from tricycle_sampling.replay.core_hours import objective
    if spec['schema'] != 'tricycle.agent-acquisition/v2' or spec['objective'] != objective(task.get('decision_protocol')):
        raise ValueError('MAE protocol or objective changed')
    for field, path in (
        ('candidate_space', directory / 'pair-candidates.json'),
        ('model_evaluation', directory / 'model-metrics.json'),
        ('sampled_set', directory.parent / 'reveal.json'),
    ):
        if spec['evidence'][field] != 'sha256:' + digest(path):
            raise ValueError(f'frozen acquisition evidence changed: {field}')
    plan = AgentAuthoredAcquisitionSamplingStrategy().plan_with_context(
        [Candidate(c['candidate_id'], c['payload']) for c in candidates],
        task['budget'], {'agent_acquisition': spec})
    definition = plan['acquisition_function']
    if definition['function_hash'] != artifact['function_hash'] or definition['function_hash'] != selection['acquisition_function_hash']:
        raise ValueError('complete acquisition definition hash mismatch')
    with (round_dir / 'model-scores.tsv').open() as handle:
        recorded = list(csv.DictReader(handle, delimiter='\t'))
    if len(recorded) != len(ids) or {row['candidate_id'] for row in recorded} != ids:
        raise ValueError('scores do not cover the complete candidate pool')
    calculated = {row['candidate_id']: row for row in plan['candidate_scores']}
    checkpoint_hash = digest(directory.parent / 'result/checkpoint.pt')
    if checkpoint_hash != request['model_snapshot']['checkpoint_hash']:
        raise ValueError('request checkpoint binding mismatch')
    if digest(directory.parent / 'result/candidate-predictions.csv') != request['model_snapshot']['score_artifact']:
        raise ValueError('candidate inference artifact changed')
    for row in recorded:
        expected = calculated[row['candidate_id']]
        if row['model_checkpoint_hash'] != checkpoint_hash:
            raise ValueError('candidate score checkpoint binding mismatch')
        for output_field, input_field in (('score', 'score'), ('acquisition_cost', 'estimated_cost')):
            if not math.isclose(float(row[output_field]), expected[input_field], rel_tol=1e-10, abs_tol=1e-12):
                raise ValueError(f'independent numeric replay mismatch: {output_field}')
    ranked = sorted(plan['candidate_scores'], key=lambda row: (-row['score'], row['candidate_id']))
    if [row['candidate_id'] for row in ranked[:task['budget']]] != selection['candidate_ids']:
        raise ValueError('selected IDs do not match the independently replayed ranking')
    model = json.loads((directory / 'model-metrics.json').read_text())
    if set(model['training_pairs']) & set(model['validation_pairs']) or len(model['validation_pairs']) != 8:
        raise ValueError('fixed auxiliary validation panel isolation failed')
    config = json.loads((directory.parents[2] / 'experiment.json').read_text())
    if set(model['validation_pairs']) != set(config['validation_panels'][str(task['seed'])]):
        raise ValueError('validation panel differs from frozen initial panel')
    from tricycle_sampling.replay import core_hours as replay_core_hours
    if spec['objective'] == replay_core_hours.OBJECTIVE:
        resource = task['context']['core_hour_budget']
        path = directory / 'revealed-cost-observations.json'
        if digest(path) != resource['calibration']['revealed_cost_observations_sha256']:
            raise ValueError('revealed cost observation binding changed')
        observations = json.loads(path.read_text())['observations']
        reveal = json.loads((directory.parent / 'reveal.json').read_text())
        replayed = json.loads(json.dumps(candidates))
        expected = replay_core_hours.cost_evidence(observations, replayed, history['selected_ids'],
                                                   reveal['cumulative_sampling_cost_core_hours'],
                                                   config['decision_protocol']['evaluation_budgets_core_hours'])
        expected['calibration']['revealed_cost_observations_sha256'] = digest(path)
        if expected != resource:
            raise ValueError('core-hour budget or calibration changed')
        if any(a['payload']['estimated_core_hours'] != b['payload']['estimated_core_hours']
               for a, b in zip(candidates, replayed, strict=True)):
            raise ValueError('candidate core-hour estimates changed')
    guidance_evidence = {}
    from tricycle_sampling.replay import cost_guidance as replay_cost_guidance
    if task.get('decision_protocol', {}).get('guidance') == replay_cost_guidance.NAME:
        reference_path = directory / 'acquisition-guidance.json'
        reference = json.loads(reference_path.read_text())
        if spec['context'].get('coverage_cost') != reference['context']:
            raise ValueError('hashed coverage/cost context differs from its reference')
        guidance_evidence = {
            'guidance_reference_sha256': digest(reference_path),
            'guidance_comparison': replay_cost_guidance.compare(reference, ranked, candidates, task['budget']),
        }
    return {**guidance_evidence, 'selection_path': str(selection_path), 'selection_sha256': digest(selection_path),
            'candidate_count': len(ids), 'selected_count': task['budget'],
            'request_sha256': digest(request_path), 'checkpoint_sha256': checkpoint_hash,
            'acquisition_function': definition, 'status': 'verified'}


def reported_training_device(path: Path) -> str | None:
    """Read the training result's device, without inferring it from warnings."""
    if not path.exists():
        return None
    device = None
    for line in path.read_text().splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get('device'), str):
            device = value['device']
    return device


def audit_lifecycle(plan: dict[str, Any]) -> dict[str, Any]:
    """Verify completed workers; live workers remain explicitly incomplete."""
    rounds = []
    incomplete = []
    failures = []
    for experiment in plan['experiments']:
        directory = Path(experiment['root'])
        config = json.loads((directory / 'experiment.json').read_text())
        try:
            check_hashes(directory / 'assets', config['assets_hashes'])
            check_hashes(directory / 'private', config['private_hashes'])
        except (OSError, ValueError) as exc:
            failures.append({'experiment': str(directory), 'error': str(exc)})
            continue
        with (directory / 'private/pair-pool.csv').open() as handle:
            pool_ids = {row['pair_id'] for row in csv.DictReader(handle)}
        for seed in sorted(directory.glob('seed-*')):
            state = json.loads((seed / 'state.json').read_text())
            if state['phase'] != 'complete':
                incomplete.append({'seed': str(seed), 'phase': state['phase']})
            previous_ids: set[str] = set()
            previous_checkpoint = None
            seen_indices = []
            for summary in sorted(seed.glob('round-*/evaluation-summary.json')):
                current = summary.parent
                try:
                    point = json.loads(summary.read_text())
                    index = int(point['round_index'])
                    if not 0 <= index < config['round_count']:
                        raise ValueError('evaluation round exceeds frozen experiment budget')
                    seen_indices.append(index)
                    check_hashes(current / 'input', json.loads((current / 'input-hashes.json').read_text()))
                    request_path = current / 'input/request.json'
                    request = json.loads(request_path.read_text())
                    if request['round_index'] != index:
                        raise ValueError('worker request round mismatch')
                    worker = json.loads((current / 'result/worker-report.json').read_text())
                    if worker['request_sha256'] != digest(request_path):
                        raise ValueError('worker request binding mismatch')
                    check_hashes(current / 'result', worker['outputs'])
                    evaluation = json.loads((current / 'result/evaluation.json').read_text())
                    if evaluation['ground_truth_sha256'] != config['evaluation_sha256']:
                        raise ValueError('evaluation snapshot mismatch')
                    for key in ('mae', 'rmse', 'n', 'ground_truth_sha256'):
                        if point[key] != evaluation[key]:
                            raise ValueError('evaluation summary differs from frozen worker output')
                    reveal = json.loads((current / 'reveal.json').read_text())
                    selected = set(reveal['selected_ids'])
                    if not selected <= pool_ids:
                        raise ValueError('selected set is outside the frozen candidate pool')
                    if len(selected) != config['batch_size']*(index+1) or len(selected) != len(reveal['selected_ids']):
                        raise ValueError('round sample budget or uniqueness mismatch')
                    if not previous_ids <= selected or set(reveal['new_ids']) != selected-previous_ids:
                        raise ValueError('label reveal history is not monotonic')
                    if len(reveal['new_ids']) != config['batch_size']:
                        raise ValueError('new batch does not match frozen batch size')
                    if index == 0 and 'initial_pair_ids' in experiment and reveal['selected_ids'] != experiment['initial_pair_ids'][state['seed']]:
                        raise ValueError('initial set differs from frozen matched set')
                    if point['selected_ids'] != reveal['selected_ids']:
                        raise ValueError('evaluation selected set differs from reveal')
                    if index == 0 and (current / 'input/previous.pt').exists():
                        raise ValueError('initial model must start from scratch')
                    if index and digest(current / 'input/previous.pt') != previous_checkpoint:
                        raise ValueError('previous checkpoint does not belong to this trajectory')
                    checkpoint = digest(current / 'result/checkpoint.pt')
                    if request['reuse_checkpoint']:
                        if checkpoint != previous_checkpoint or reveal['new_label_rows'] != 0:
                            raise ValueError('checkpoint reuse is inconsistent with new labels')
                    else:
                        summaries = list((current / 'result/full-fit').glob('**/*summary.json'))
                        if len(summaries) != 1:
                            raise ValueError('missing unambiguous full-model training summary')
                        training = json.loads(summaries[0].read_text())
                        if training['epochs_completed'] != config['training']['epochs']:
                            raise ValueError('full model did not complete the frozen training epochs')
                    previous_ids = selected
                    previous_checkpoint = checkpoint
                    training_log = current / 'result/full-fit/train.log'
                    rounds.append({'round': str(current), 'sampled_pairs': len(selected),
                                   'checkpoint_sha256': checkpoint, 'status': 'verified',
                                   'requested_device': config['training'].get('device'),
                                   'reported_training_device': 'checkpoint-reuse' if request['reuse_checkpoint'] else reported_training_device(training_log),
                                   'training_log_sha256': digest(training_log) if training_log.exists() else None})
                except (OSError, ValueError, KeyError) as exc:
                    failures.append({'round': str(current), 'error': str(exc)})
            if seen_indices != list(range(len(seen_indices))):
                failures.append({'seed': str(seed), 'error': 'evaluation sequence has missing or duplicate rounds'})
            if state['phase'] == 'complete' and len(seen_indices) != config['round_count']:
                failures.append({'seed': str(seed), 'error': 'complete state lacks the full evaluation trajectory'})
            if state['phase'] == 'complete' and (set(state['selected_ids']) != pool_ids or previous_ids != pool_ids):
                failures.append({'seed': str(seed), 'error': 'complete trajectory has not covered the full pool'})
    device_counts: dict[str, int] = {}
    for row in rounds:
        device = row['reported_training_device'] or 'unknown'
        device_counts[device] = device_counts.get(device, 0) + 1
    return {'verified_rounds': len(rounds), 'rounds': rounds, 'failures': failures,
            'reported_training_devices': device_counts,
            'cpu_training_rounds': [r['round'] for r in rounds if r['reported_training_device'] == 'cpu'],
            'incomplete_trajectories': incomplete,
            'complete': not failures and not incomplete and len(rounds) == plan['maximum_sar_jobs']}


def audit_suite(root: Path, *, require_complete: bool = False) -> dict[str, Any]:
    plan = verified_plan(root)
    decisions = []
    failures = []
    for selection in sorted(root.glob('*/seed-*/round-*/decision/selection.json')):
        try:
            item = audit_decision(selection)
            relative = selection.relative_to(root)
            target = root / 'audit/full-functions' / relative.parent / 'definition.json'
            save(target, item)
            decisions.append({k: v for k, v in item.items() if k != 'acquisition_function'})
        except (OSError, ValueError, KeyError) as exc:
            failures.append({'selection_path': str(selection), 'error': str(exc)})
    lifecycle = audit_lifecycle(plan)
    failures.extend(lifecycle['failures'])
    if require_complete and not lifecycle['complete']:
        failures.append({'error': 'full frozen experiment budget has not completed'})
    if require_complete:
        expected_decisions = plan['maximum_sar_jobs'] - plan['seeds_per_strategy']*len(plan['experiments'])
        if len(decisions) != expected_decisions:
            failures.append({'error': 'full committed selection history is missing'})
    result = {'schema': 'tricycle.mae-audit/v1', 'audit_tool_sha256': digest(Path(__file__)),
              'verified_decisions': len(decisions),
              'verified_candidate_scores': sum(d['candidate_count'] for d in decisions),
              'failures': failures, 'decisions': decisions,
              'lifecycle': lifecycle,
              'scope': 'committed decisions and completed workers present at audit time'}
    save(root / 'audit/independent-replay.json', result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--require-complete', action='store_true')
    args = parser.parse_args()
    result = audit_suite(args.run_dir.resolve(), require_complete=args.require_complete)
    print({k: v for k, v in result.items() if k not in ('decisions', 'lifecycle')})
    print({k: v for k, v in result['lifecycle'].items() if k != 'rounds'})
    if result['failures']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
