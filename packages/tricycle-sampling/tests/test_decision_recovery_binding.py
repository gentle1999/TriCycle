from pathlib import Path

import pytest
from tricycle_sampling.replay import campaign
from tricycle_sampling.replay.decision import attempt_limit


def test_recovery_allowance_preserves_counter_and_binds_original_failures(tmp_path: Path) -> None:
    campaign.save(tmp_path / 'task.json', {'round': 8})
    campaign.save(tmp_path / 'correction-feedback.json', {'rejected_attempts': 3})
    feedback = (tmp_path / 'correction-feedback.json').read_bytes()
    assert attempt_limit(tmp_path, 3) == 3
    failures = {}
    for index in range(1, 4):
        path = tmp_path / 'rejected-attempts' / f'{index:04d}' / 'rejection.json'
        campaign.save(path, {'error': 'invalid retain', 'attempt': index})
        failures[str(index)] = campaign.digest(path)
    record = {'schema': 'tricycle.decision-recovery/v1',
              'task_sha256': campaign.digest(tmp_path / 'task.json'),
              'authorization': 'Explicit repair request', 'reason': 'Correct ambiguous retry context',
              'original_maximum': 3, 'additional_attempts': 3, 'original_rejections': failures}
    campaign.save(tmp_path / 'recovery-authorization.json', record)
    assert attempt_limit(tmp_path, 3) == 6
    assert (tmp_path / 'correction-feedback.json').read_bytes() == feedback
    record['additional_attempts'] = 4
    campaign.save(tmp_path / 'recovery-authorization.json', record)
    with pytest.raises(ValueError, match='authorization'):
        attempt_limit(tmp_path, 3)
    record['additional_attempts'] = 3
    campaign.save(tmp_path / 'recovery-authorization.json', record)
    campaign.save(tmp_path / 'rejected-attempts/0001/rejection.json', {'error': 'rewritten'})
    with pytest.raises(ValueError, match='evidence changed'):
        attempt_limit(tmp_path, 3)


def test_controller_amendment_preserves_frozen_config_and_rejects_unreviewed_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, root = tmp_path / 'source', tmp_path / 'run'
    module = source / 'replay/mae.py'
    module.parent.mkdir(parents=True)
    module.write_text('original prompt')
    before = campaign.digest(module)
    campaign.save(root / 'experiment.json', {'controller_hashes': {'replay/mae.py': before}})
    frozen = (root / 'experiment.json').read_bytes()
    monkeypatch.setattr(campaign, 'PACKAGE_ROOT', source)
    module.write_text('clarified prompt')
    with pytest.raises(ValueError, match='frozen artifact'):
        campaign.check_controller(root)
    evidence = tmp_path / 'evidence.json'
    campaign.save(evidence, {'review': 'prompt change only'})
    record = {'schema': 'tricycle.decision-controller-amendment/v1',
              'experiment_sha256': campaign.digest(root / 'experiment.json'),
              'authorization': 'Explicit repair request', 'reason': 'Clarify rejected proposal',
              'evidence_path': str(evidence), 'evidence_sha256': campaign.digest(evidence),
              'changes': {'replay/mae.py': {'before': before, 'after': campaign.digest(module)}}}
    campaign.save(root / 'decision-controller-amendment.json', record)
    campaign.check_controller(root)
    assert (root / 'experiment.json').read_bytes() == frozen
    module.write_text('unreviewed change')
    with pytest.raises(ValueError, match='frozen artifact'):
        campaign.check_controller(root)
    record['changes'] = {'replay/worker.py': {'before': before, 'after': before}}
    campaign.save(root / 'decision-controller-amendment.json', record)
    with pytest.raises(ValueError, match='scope is invalid'):
        campaign.check_controller(root)
