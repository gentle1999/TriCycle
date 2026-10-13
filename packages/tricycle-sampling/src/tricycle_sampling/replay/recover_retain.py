"""Reconcile explicit retain intent only with exact full-pool numeric equivalence.

No new proposal invocation, retry reset, formula preference or selection change.
Default is read-only. Live recovery performs the originally authorized review
once and hands a validated response to the frozen select phase.
"""
from __future__ import annotations

import argparse
import fcntl
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
from tricycle_sampling.replay.decision import DecisionOutputError, decision_spec, validate
from tricycle_sampling.replay.suite import verified_plan


def canonical_retain(directory: Path, raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if raw.get('decision_action') != 'retain':
        raise ValueError('reconciliation requires explicit retain intent')
    task = read(directory / 'task.json')
    previous = read(directory / 'acquisition-history.json')['previous_rounds'][-1]['function']
    bound = {**raw, 'task_sha256': digest(directory / 'task.json')}
    canonical = replay_mae.compile_response(
        {**bound, **{key: previous[key] for key in ('expression', 'cost_expression')}}, task, previous)
    candidates = [Candidate(c['candidate_id'], c['payload'])
                  for c in read(directory / 'pair-candidates.json')['candidates']]
    strategy = AgentAuthoredAcquisitionSamplingStrategy()
    # Action adjust is diagnostic only: the raw retained shorthand would fail
    # textual validation. No candidate is selected with this counterfactual.
    diagnostic = replay_mae.compile_response({**bound, 'decision_action': 'adjust'}, task, previous)
    raw_plan = strategy.plan_with_context(candidates, task['budget'], {
        'agent_acquisition': decision_spec(directory, diagnostic)})
    exact_plan = strategy.plan_with_context(candidates, task['budget'], {
        'agent_acquisition': decision_spec(directory, canonical)})
    rows = []
    raw_scores = {r['candidate_id']: r for r in raw_plan['candidate_scores']}
    for expected in exact_plan['candidate_scores']:
        original = raw_scores[expected['candidate_id']]
        if any(original[key] != expected[key] for key in ('score', 'estimated_cost')):
            raise ValueError('retained shorthand changes scores or costs on the current pool')
        rows.append({key: expected[key] for key in ('candidate_id', 'score', 'estimated_cost')})
    def ranking(plan: dict[str, Any]) -> list[str]:
        return [r['candidate_id'] for r in sorted(plan['candidate_scores'],
                                                key=lambda r: (-r['score'], r['candidate_id']))]
    if ranking(raw_plan) != ranking(exact_plan):
        raise ValueError('retained shorthand changes ranking')
    canonical['decision_source'] = 'agent-retain-canonicalization'
    return canonical, {'candidate_scores': rows, 'ranked_ids': ranking(exact_plan),
                       'selected_ids': ranking(exact_plan)[:task['budget']],
                       'equivalence': 'exact float scores and costs for every remaining candidate; identical full ranking',
                       'original_expression': raw['expression'], 'canonical_expression': canonical['expression']}


def recover(directory: Path, *, live: bool = False) -> dict[str, Any]:
    directory = directory.resolve()
    seed = directory.parents[1]
    experiment = seed.parent
    verified_plan(experiment.parent)
    state = read(seed / 'state.json')
    if (state['phase'] != 'blocked' or state.get('blocked_phase') != 'agent'
            or directory.parent != seed / f"round-{state['round']:04d}"):
        raise ValueError('requires the current blocked agent round')
    feedback = read(directory / 'correction-feedback.json')
    config = read(experiment / 'experiment.json')
    maximum = int(config.get('max_decision_attempts', 3))
    if feedback['rejected_attempts'] != maximum or not config['decision_protocol']['review']:
        raise ValueError('requires exhausted proposal corrections and one authorized review')
    proposal_path = directory / 'rejected-attempts' / f'{maximum:04d}' / 'response.pending.json'
    rejection = read(proposal_path.parent / 'rejection.json')
    if (rejection['task_sha256'] != digest(directory / 'task.json')
            or rejection['error'] != 'retain must preserve the previous expression exactly'
            or read(proposal_path.parent / 'agent-exit.json')['returncode'] != 0
            or read(proposal_path.parent / 'agent-inflight.json')['task_sha256'] != rejection['task_sha256']):
        raise ValueError('original completed proposal does not bind this recoverable retain error')
    canonical, proof = canonical_retain(directory, read(proposal_path))
    if not live:
        return {'status': 'read-only exact equivalence', 'candidate_count': len(proof['candidate_scores']),
                'proposal_attempts': maximum, 'new_proposal_calls': 0}
    with (seed / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if read(seed / 'state.json') != state:
            raise ValueError('trajectory changed during diagnosis')
        check_hashes(directory, read(directory / 'evidence-hashes.json'))
        check_hashes(directory.parent / 'input', read(directory.parent / 'input-hashes.json'))
        check_hashes(directory.parent / 'result', read(directory.parent / 'result/worker-report.json')['outputs'])
        archive = directory.parent / 'retain-recovery/attempt-0001'
        record = archive / 'operation.json'
        binding = {'task_sha256': digest(directory / 'task.json'), 'proposal_sha256': digest(proposal_path),
                   'correction_feedback_sha256': digest(directory / 'correction-feedback.json'),
                   'review_source_sha256': digest(Path(replay_mae.__file__)),
                   'recovery_source_sha256': digest(Path(__file__))}
        if record.exists():
            if read(record) != binding or read(archive / 'blocked-state.json') != state:
                raise ValueError('recovery archive does not bind this operation')
        else:
            if any(directory.glob('review-agent*')) or (directory / 'response.json').exists():
                raise ValueError('unowned response or review already exists')
            archive.mkdir(parents=True, exist_ok=False)
            save(archive / 'blocked-state.json', state)
            save(archive / 'original-proposal.json', read(proposal_path))
            save(archive / 'equivalence.json', proof)
            save(record, binding)
        validate(directory, canonical)
        save(directory / 'response.json', canonical)
        replay_mae.review(directory, canonical)
        save(directory / 'proposal-response.json', canonical)
        reviewed_path = directory / 'review-response.json'
        if not reviewed_path.exists():
            try:
                reviewed_path = _run_agent_once(directory / 'task.json', config, review_pass=True)
            except DecisionOutputError as exc:
                if str(exc) != 'retain must preserve the previous expression exactly':
                    raise
                pending = directory / 'review-response.pending.json'
                if (read(directory / 'review-agent-exit.json')['returncode'] != 0
                        or read(directory / 'review-agent-inflight.json')['task_sha256'] != binding['task_sha256']):
                    raise ValueError('review invocation is not a finished bound response') from exc
                normalized, review_proof = canonical_retain(directory, read(pending))
                save(archive / 'original-review.json', read(pending))
                save(archive / 'review-equivalence.json', review_proof)
                save(reviewed_path, normalized)
        reviewed = read(reviewed_path)
        validate(directory, reviewed)
        save(directory / 'response.json', reviewed)
        save(directory / 'review-approved.json', {'proposal_sha256': digest(directory / 'proposal-response.json'),
             'response_sha256': digest(directory / 'response.json'), 'review_passes': 1})
        save(archive / 'completed.json', {'response_sha256': digest(directory / 'response.json'),
             'new_proposal_calls': 0, 'proposal_attempts': maximum, 'review_passes': 1})
        # All scientific validation and the sole review have finished. Preserve
        # the exhausted counter; the original select phase still revalidates.
        state.update({'phase': 'select', 'next_check': 0, 'failures': 0})
        state.pop('blocked_phase', None)
        state.pop('last_error', None)
        save_state(seed, state)
    return {'status': 'retained intent reconciled and reviewed; original select phase ready'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--decision-dir', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    print(recover(args.decision_dir, live=args.live))


if __name__ == '__main__':
    main()
