"""Validate and audit bounded correction of unselected model responses."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tricycle_core import record_io as rio
from tricycle_core.record_io import file_sha256 as digest
from tricycle_core.record_io import write_json as save

from tricycle_sampling import AgentAuthoredAcquisitionSamplingStrategy, Candidate


class DecisionOutputError(ValueError):
    """A finished model call produced correctable output, not a remote job error."""


class DecisionTimeoutError(RuntimeError):
    """A terminated invocation may restart without consuming scientific corrections."""

    def __init__(self, message: str, *, review_pass: bool = False) -> None:
        super().__init__(message)
        self.review_pass = review_pass
        self.restart_available = False


def archive_timeout(directory: Path, error: DecisionTimeoutError) -> bool:
    with rio.transaction(directory):
        prefix = 'review-' if error.review_pass else ''
        root = directory / 'timeout-attempts' / ('review' if error.review_pass else 'proposal')
        count = len(list(rio.glob(root, '*/timeout.json'))) + 1
        archive = root / f'{count:04d}'
        rio.mkdir(archive, parents=True, exist_ok=False)
        for name in ('agent.log', 'agent-exit.json', 'agent-inflight.json', 'agent-process.json',
                     'response.pending.json', 'response-schema.json', 'decision-input.json', 'recovered-output.json'):
            source = directory / (prefix + name)
            if rio.exists(source):
                rio.move(str(source), archive / source.name)
        save(archive / 'timeout.json', {'error': str(error), 'task_sha256': digest(directory / 'task.json'),
                                      'attempt': count, 'maximum_restarts': 3})
        return count <= 3


def attempt_limit(directory: Path, configured: int) -> int:
    """Honor one explicit, task-bound recovery allowance without resetting history."""
    path = directory / 'recovery-authorization.json'
    if not rio.exists(path):
        return configured
    record = read(path)
    extra = record.get('additional_attempts')
    if (record.get('schema') != 'tricycle.decision-recovery/v1'
            or record.get('task_sha256') != digest(directory / 'task.json')
            or record.get('original_maximum') != configured
            or not record.get('authorization') or not record.get('reason')
            or type(extra) is not int or not 1 <= extra <= 3):
        raise ValueError('Invalid decision recovery authorization')
    for index in range(1, configured + 1):
        rejection = directory / 'rejected-attempts' / f'{index:04d}' / 'rejection.json'
        if record.get('original_rejections', {}).get(str(index)) != digest(rejection):
            raise ValueError('Original rejected decision evidence changed')
    return configured + extra


def read(path: Path) -> dict[str, Any]:
    return dict(json.loads(rio.read_text(path)))


def decision_spec(directory: Path, response: dict[str, Any]) -> dict[str, Any]:
    task = read(directory / 'task.json')
    if response.get('task_sha256') != digest(directory / 'task.json'):
        raise DecisionOutputError('task_sha256 must exactly match the task hash supplied in the prompt')
    for name in ('expression', 'cost_expression', 'rationale', 'author_notes'):
        if not isinstance(response.get(name), str) or not response[name].strip():
            raise DecisionOutputError(f'missing non-empty response field: {name}')
    evidence: dict[str, Any] = {
        'sampled_set': digest(directory.parent / 'reveal.json'),
        'candidate_space': digest(directory / 'pair-candidates.json'),
        'model_evaluation': digest(directory / 'model-metrics.json'),
    }
    for key, name in [('calculation_status', 'status.tsv'), ('downloaded_results', 'download-results.tsv')]:
        path = directory.parent / name
        evidence[key] = {'manifest_path': str(path), 'sha256': digest(path)}
    spec = {**response, 'schema': 'tricycle.agent-acquisition/v1', 'function_id': 'response-preflight',
            'version': '1.0.0', 'authored_by': 'agent-authored-acquisition',
            'context': task['context'], 'evidence': evidence}
    if task.get('decision_protocol'):
        spec['schema'] = 'tricycle.agent-acquisition/v2'
        from tricycle_sampling.replay.mae import compile_response
        history = read(directory / 'acquisition-history.json')['previous_rounds']
        try:
            compiled = compile_response(response, task, history[-1]['function'] if history else None)
            if response.get('objective') != compiled['objective']:
                raise ValueError('response objective differs from frozen task objective')
        except ValueError as exc:
            raise DecisionOutputError(str(exc)) from exc
    return spec


def validate(directory: Path, response: dict[str, Any]) -> None:
    task = read(directory / 'task.json')
    spec = decision_spec(directory, response)
    evidence = spec['evidence']
    candidates = [Candidate(c['candidate_id'], c['payload'])
                  for c in read(directory / 'pair-candidates.json')['candidates']]
    # Evidence failures must not be classified as model mistakes.
    from tricycle_sampling import validate_calculation_evidence
    validate_calculation_evidence(evidence)
    try:
        strategy = AgentAuthoredAcquisitionSamplingStrategy()
        strategy.validate_with_context(candidates, task['budget'], {'agent_acquisition': spec})
        plan = strategy.plan_with_context(candidates, task['budget'], {'agent_acquisition': spec})
    except ValueError as exc:
        raise DecisionOutputError(str(exc)) from exc
    ranked = sorted(plan['candidate_scores'], key=lambda row: (-float(row['score']), row['candidate_id']))
    if task.get('decision_protocol') and len(ranked) > task['budget'] and len({float(row['score']) for row in ranked}) == 1:
        raise DecisionOutputError('acquisition produces constant scores; author a discriminating rule')
    assessment = {
        'task_sha256': digest(directory / 'task.json'),
        'cost_summary': plan['acquisition_function']['cost_summary'],
        'candidate_count': len(candidates),
        'score_min': min(float(row['score']) for row in ranked),
        'score_max': max(float(row['score']) for row in ranked),
        'proposed_selected_ids': [row['candidate_id'] for row in ranked[:task['budget']]],
        'status': 'preflight-only; final selection is recorded by exploration.round',
    }
    from tricycle_sampling.replay import core_hours as replay_core_hours
    if task.get('decision_protocol', {}).get('name') == replay_core_hours.NAME:
        assessment['resource_diagnostics'] = replay_core_hours.ranking_diagnostics(
            ranked, task['budget'], read(directory / 'pair-candidates.json')['candidates'])
        assessment['resource_diagnostics']['cost_unit'] = 'core_hours'
    from tricycle_sampling.replay import cost_guidance as replay_cost_guidance
    if task.get('decision_protocol', {}).get('guidance') == replay_cost_guidance.NAME:
        reference = read(directory / 'acquisition-guidance.json')
        if task['context'].get('coverage_cost') != reference['context']:
            raise ValueError('coverage/cost context differs from its frozen reference')
        assessment['guidance_comparison'] = replay_cost_guidance.compare(
            reference, ranked, read(directory / 'pair-candidates.json')['candidates'], task['budget'])
    save(directory / 'formula-assessment.json', assessment)


def reject(directory: Path, error: str, *, maximum: int = 3) -> bool:
    """Preserve every rejected output; never modify a completed decision."""
    with rio.transaction(directory):
        task = read(directory / 'task.json')
        round_dir = directory.parent.parent / 'exploration/rounds' / f'{task["round"]:04d}'
        if rio.exists(directory / 'selection.json') or rio.exists(round_dir / 'round.toml'):
            raise ValueError('refusing to revise a completed selection')
        feedback = directory / 'correction-feedback.json'
        attempts = int(read(feedback).get('rejected_attempts', 0)) if rio.exists(feedback) else 0
        if attempts >= maximum:
            return False
        archive = directory / 'rejected-attempts' / f'{attempts + 1:04d}'
        rio.mkdir(archive, parents=True, exist_ok=False)
        for name in ('response.json', 'response.pending.json', 'accepted-response.json',
                     'selection-request.toml', 'agent-inflight.json', 'agent-process.json',
                     'agent-exit.json', 'agent.log', 'response-schema.json',
                     'ACQUISITION.md', 'acquisition-summary.json', 'rationale-brief.json',
                     'decision-input.json', 'decision-brief.json', 'decision-validation.json', 'formula-assessment.json',
                     'proposal-response.json', 'proposal-review.json', 'review-approved.json',
                     'review-response.json', 'review-response.pending.json', 'review-response-schema.json',
                     'review-agent-inflight.json', 'review-agent-process.json', 'review-agent-exit.json',
                     'review-agent.log', 'review-decision-input.json', 'recovered-output.json', 'review-recovered-output.json'):
            path = directory / name
            if rio.exists(path):
                rio.move(str(path), archive / name)
        if rio.exists(round_dir):
            rio.move(str(round_dir), archive / 'incomplete-exploration')
        save(archive / 'rejection.json', {'error': error, 'task_sha256': digest(directory / 'task.json')})
        save(feedback, {'rejected_attempts': attempts + 1, 'maximum_attempts': maximum,
                        'error': error, 'previous_output_directory': str(archive.relative_to(directory)),
                        'instruction': 'Correct the concrete validation error; preserve scientific intent where possible. Recheck the full numeric DSL and exact task hash.'})
        return attempts + 1 < maximum
