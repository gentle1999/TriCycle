from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tricycle_bohrsubmit.gateway import BohrGatewayError
from tricycle_sampling.replay import campaign
from tricycle_sampling.replay.decision import DecisionTimeoutError, archive_timeout


def submission(tmp_path: Path) -> Path:
    campaign.save(tmp_path / 'submission-inflight.json', {
        'job_name': 'seed-0-round-3', 'started_at': 1, 'timeout_error': 'UPSTREAM_TIMEOUT'})
    campaign.save(tmp_path / 'job-spec.json', {
        'name': 'seed-0-round-3', 'parameters': {'job_group_id': '42'}})
    return tmp_path


def test_existing_job_reconciles_without_resubmission(tmp_path: Path) -> None:
    current = submission(tmp_path)
    gateway = Mock()
    gateway.reconcile_submission.return_value = {'status': 'found', 'external_id': '99'}
    assert campaign.reconcile_timeout_submission(current, gateway)
    assert campaign.read(current / 'submission.json')['external_id'] == '99'
    gateway.submit.assert_not_called()


def test_absence_needs_two_spaced_checks_and_retains_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current = submission(tmp_path)
    gateway = Mock()
    gateway.reconcile_submission.return_value = {'status': 'absent'}
    now = [100.0]
    monkeypatch.setattr(campaign.time, 'time', lambda: now[0])
    assert not campaign.reconcile_timeout_submission(current, gateway)
    now[0] = 159
    assert not campaign.reconcile_timeout_submission(current, gateway)
    now[0] = 160
    assert campaign.reconcile_timeout_submission(current, gateway)
    assert not (current / 'submission-inflight.json').exists()
    assert (current / 'submission-timeouts/0001/submission-inflight.json').exists()
    assert not (current / 'submission.json').exists()


def test_unknown_submission_and_exhausted_restarts_remain_recoverable(tmp_path: Path) -> None:
    current = submission(tmp_path)
    gateway = Mock()
    gateway.reconcile_submission.side_effect = BohrGatewayError('incomplete inventory')
    original = (current / 'submission-inflight.json').read_bytes()
    with pytest.raises(BohrGatewayError):
        campaign.reconcile_timeout_submission(current, gateway)
    assert (current / 'submission-inflight.json').read_bytes() == original
    gateway.reconcile_submission.side_effect = None
    gateway.reconcile_submission.return_value = {'status': 'absent'}
    for index in range(1, 4):
        campaign.save(current / f'submission-timeouts/{index:04d}/submission-inflight.json', {})
    with pytest.raises(ValueError, match='restarts exhausted'):
        campaign.reconcile_timeout_submission(current, gateway)
    assert (current / 'submission-inflight.json').exists()


def test_submit_timeout_leaves_stage_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed = tmp_path / 'seed-00'
    current = seed / 'round-0000'
    campaign.save(seed / 'state.json', {'seed': 0, 'round': 0, 'revision': 0, 'phase': 'submit', 'failures': 0})
    campaign.save(tmp_path / 'experiment.json', {})
    campaign.save(tmp_path / 'experiment-hash.json', {'sha256': campaign.digest(tmp_path / 'experiment.json')})
    monkeypatch.setattr(campaign, 'verify_inputs', lambda _: None)
    monkeypatch.setattr(campaign, 'ensure_job_group', lambda *args: '42')
    spec = SimpleNamespace(name='stable', to_dict=lambda: {'name': 'stable', 'parameters': {'job_group_id': '42'}})
    monkeypatch.setattr(campaign, 'job_spec', lambda *args, **kwargs: spec)
    gateway = Mock()
    gateway.submit.side_effect = BohrGatewayError('UPSTREAM_TIMEOUT: deadline exceeded')
    state = campaign.tick(tmp_path, seed, gateway, allow_submit=True)
    assert state['phase'] == 'submit'
    assert state['next_check'] > 0
    assert campaign.read(current / 'submission-inflight.json')['timeout_error']


def test_review_timeout_preserves_valid_proposal_and_scientific_counter(tmp_path: Path) -> None:
    campaign.save(tmp_path / 'task.json', {'round': 3})
    campaign.save(tmp_path / 'response.json', {'expression': 'valid proposal'})
    campaign.save(tmp_path / 'correction-feedback.json', {'rejected_attempts': 2})
    before = (tmp_path / 'response.json').read_bytes()
    for index in range(1, 5):
        campaign.save(tmp_path / 'review-agent-exit.json', {'error': 'timeout'})
        campaign.save(tmp_path / 'review-agent-inflight.json', {'task_sha256': campaign.digest(tmp_path / 'task.json')})
        assert archive_timeout(tmp_path, DecisionTimeoutError('timed out', review_pass=True)) == (index <= 3)
    assert (tmp_path / 'response.json').read_bytes() == before
    assert campaign.read(tmp_path / 'correction-feedback.json')['rejected_attempts'] == 2
    assert not (tmp_path / 'review-agent-inflight.json').exists()
    assert len(list(tmp_path.glob('timeout-attempts/review/*/timeout.json'))) == 4
