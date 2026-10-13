"""Recover an interrupted review when only the previous-rule diagnostic is tied.

The active proposal keeps the frozen sampling validation and budget. A previous
formula is evaluated over the whole pool for diagnosis, never accepted as a new
sampling rule. Default invocation is local and read-only.
"""
from __future__ import annotations

import argparse
import fcntl
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from tricycle_sampling import AgentAuthoredAcquisitionSamplingStrategy, Candidate
from tricycle_sampling.replay import mae as replay_mae
from tricycle_sampling.replay.campaign import (
    _run_agent_once,
    check_hashes,
    digest,
    read,
    save,
    save_state,
)
from tricycle_sampling.replay.decision import decision_spec, validate
from tricycle_sampling.replay.suite import verified_plan


def diagnostic_review(directory: Path, response: dict[str, Any] | None = None) -> dict[str, Any]:
    task = read(directory / 'task.json')
    response = read(directory / 'response.json') if response is None else response
    candidates = [Candidate(c['candidate_id'], c['payload'])
                  for c in read(directory / 'pair-candidates.json')['candidates']]
    spec = decision_spec(directory, response)
    strategy = AgentAuthoredAcquisitionSamplingStrategy()
    plan = strategy.plan_with_context(candidates, task['budget'], {'agent_acquisition': spec})
    ranked = sorted(plan['candidate_scores'], key=lambda row: (-row['score'], row['candidate_id']))
    ids = [row['candidate_id'] for row in ranked[:task['budget']]]
    selected = [c for c in candidates if c.candidate_id in set(ids)]
    old = read(directory / 'acquisition-history.json')['previous_rounds'][-1]['function']
    old_spec = {**spec, 'expression': old['expression'], 'cost_expression': old['cost_expression']}
    # A counterfactual diagnosis has no selection side effect. The old formula
    # may legitimately lose discrimination as the remaining pool changes.
    previous = strategy.plan_with_context(candidates, len(candidates), {'agent_acquisition': old_spec})
    old_ranked = sorted(previous['candidate_scores'], key=lambda row: (-row['score'], row['candidate_id']))
    old_unique = len({row['score'] for row in old_ranked})
    if old_unique != 1 or len(candidates) <= task['budget']:
        raise ValueError('recovery applies only to a tied previous-rule diagnostic with a valid new proposal')
    old_ids = {row['candidate_id'] for row in old_ranked[:task['budget']]}
    numeric = [key for key, value in candidates[0].payload.items() if isinstance(value, (int, float))]
    distributions = {key: {'pool_mean': sum(c.payload[key] for c in candidates)/len(candidates),
                           'selected_mean': sum(c.payload[key] for c in selected)/len(selected)}
                     for key in numeric}
    counts: dict[str, int] = defaultdict(int)
    for candidate in selected:
        counts[replay_mae.region(dict(candidate.payload))] += 1
    return {
        'proposal': {key: response[key] for key in replay_mae.FIELDS}, 'selected_count': len(ids),
        'selected_region_counts': dict(counts), 'feature_comparison': distributions,
        'score_min': ranked[-1]['score'], 'score_max': ranked[0]['score'],
        'unique_scores': len({row['score'] for row in ranked}),
        'previous_rule_on_current_pool_overlap': len(old_ids & set(ids)), 'batch_size': task['budget'],
        'previous_rule_diagnostic': {'unique_scores': old_unique,
            'score_min': old_ranked[-1]['score'], 'score_max': old_ranked[0]['score'],
            'selection_interpretation': 'No score preference: the counterfactual batch uses candidate_id ties only.',
            'role': 'diagnostic only; not an accepted new sampling rule'},
        'batch_similarity': replay_mae.batch_similarity([str(c.payload['mapped_reaction_smiles']) for c in selected]),
        'selected_examples': [{'candidate_id': c.candidate_id, 'reaction_smiles': c.payload['mapped_reaction_smiles']}
                              for c in selected[:6]],
        'limits': 'No labels or generalization outcome for the proposed batch are available. Previous-rule overlap is determined solely by ID ties.',
        'proposal_sha256': digest(directory / 'proposal-response.json' if
                                  (directory / 'proposal-response.json').exists() else directory / 'response.json'),
    }


def recover(directory: Path, *, live: bool = False) -> dict[str, Any]:
    directory = directory.resolve()
    seed = directory.parents[1]
    experiment = seed.parent
    root = experiment.parent
    plan = verified_plan(root)
    if not any(Path(e['root']) == experiment and e['variant'] == 'reviewed' for e in plan['experiments']):
        raise ValueError('recovery requires a frozen reviewed trajectory')
    state = read(seed / 'state.json')
    archive = directory.parent / 'review-recovery/attempt-0001'
    repair_path = archive / 'repair.json'
    repair = read(repair_path) if repair_path.exists() else None
    if (state['phase'] != 'blocked' or state.get('blocked_phase') != 'agent'
            or directory.parent != seed / f"round-{state['round']:04d}"):
        raise ValueError('recovery requires the current blocked agent round')
    if repair is not None:
        if (read(archive / 'blocked-state.json') != state
                or repair['task_sha256'] != digest(directory / 'task.json')
                or repair['recovery_source_sha256'] != digest(Path(__file__))
                or repair['review_source_sha256'] != digest(Path(replay_mae.__file__))
                or repair['proposal_sha256'] != digest(archive / 'proposal-response.json')):
            raise ValueError('recovery archive does not bind this operation')
        assessment = repair['assessment']
        proposal = read(archive / 'proposal-response.json')
    else:
        assessment = diagnostic_review(directory)
        proposal = read(directory / 'response.json')
    if not live:
        return {'proposal_unique_scores': assessment['unique_scores'],
                'previous_unique_scores': assessment['previous_rule_diagnostic']['unique_scores'],
                'status': 'read-only diagnosis'}
    with (seed / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if read(seed / 'state.json') != state:
            raise ValueError('trajectory changed during diagnosis')
        config = read(experiment / 'experiment.json')
        if not config['decision_protocol']['review']:
            raise ValueError('the frozen protocol does not authorize review')
        check_hashes(directory, read(directory / 'evidence-hashes.json'))
        check_hashes(directory.parent / 'input', read(directory.parent / 'input-hashes.json'))
        worker = read(directory.parent / 'result/worker-report.json')
        check_hashes(directory.parent / 'result', worker['outputs'])
        validate(directory, proposal)
        if (directory / 'review-approved.json').exists() and repair is None:
            raise ValueError('a review was already approved; inspect it without creating another pass')
        if repair is None:
            if any(directory.glob('review-agent*')) or (directory / 'review-response.json').exists():
                raise ValueError('an unowned review invocation already exists; reconcile it first')
            archive.mkdir(parents=True, exist_ok=False)
            save(archive / 'blocked-state.json', state)
            save(archive / 'proposal-response.json', proposal)
            save(archive / 'repair.json', {'proposal_sha256': assessment['proposal_sha256'],
             'task_sha256': digest(directory / 'task.json'),
             'review_source_sha256': digest(Path(replay_mae.__file__)),
             'recovery_source_sha256': digest(Path(__file__)),
             'reason': 'Old-rule counterfactual has tied scores; the new proposal is discriminating.',
             'assessment': assessment})
        elif digest(directory / 'response.json') not in (
                repair['proposal_sha256'],
                digest(directory / 'review-response.json') if (directory / 'review-response.json').exists() else ''):
            raise ValueError('active response changed outside the archived review')
        save(directory / 'proposal-review.json', assessment)
        save(directory / 'proposal-response.json', proposal)
        started = time.monotonic()
        reviewed = _run_agent_once(directory / 'task.json', config, review_pass=True)
        validate(directory, read(reviewed))
        save(directory / 'response.json', read(reviewed))
        save(directory / 'review-approved.json', {'proposal_sha256': assessment['proposal_sha256'],
             'response_sha256': digest(directory / 'response.json'), 'review_passes': 1})
        save(archive / 'completed.json', {'review_seconds': time.monotonic()-started,
             'approved_response_sha256': digest(directory / 'response.json')})
        state.update({'phase': 'agent', 'next_check': 0, 'failures': 0})
        state.pop('blocked_phase', None)
        state.pop('last_error', None)
        save_state(seed, state)
    return {'status': 'single review completed; ready for the original controller', 'round': state['round']}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--decision-dir', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    print(recover(args.decision_dir, live=args.live))


if __name__ == '__main__':
    main()
