from __future__ import annotations

import argparse
import csv
from pathlib import Path

import pytest
from tricycle_core import BohrJobSpec
from tricycle_core.toml import dump_toml, load_toml
from tricycle_sampling.active_learning import _main_submit


class Gateway:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.submissions: list[BohrJobSpec] = []
        self.groups: list[tuple[str, str]] = []
        self.fail_first = fail_first

    def create_job_group(self, name: str, project_id: str) -> str:
        self.groups.append((name, project_id))
        return 'group-test'

    def submit(self, job: BohrJobSpec) -> str:
        self.submissions.append(job)
        if self.fail_first and len(self.submissions) == 1:
            raise TimeoutError('submission receipt lost')
        return f'job-{len(self.submissions)}'

    def status(self, external_id: str) -> dict[str, str]:
        raise AssertionError('no remote status query is expected')

    def download(self, external_id: str, destination: Path) -> tuple[str, ...]:
        raise AssertionError('no download is expected')

    def terminate(self, external_id: str) -> str:
        raise AssertionError('no termination is expected')


def prepared(tmp_path: Path) -> argparse.Namespace:
    rows = []
    for project in ('autode', 'dpa-fined-rits-da', 'rits-zero-shot-da'):
        for seed in range(5):
            name = f'{project}-{seed}'
            request = tmp_path / 'requests' / f'{name}.toml'
            dump_toml(request, {'image': 'test-image', 'command': ['bash', 'run.sh'],
                               'job_name': name, 'project_id': 'test-project',
                               'machine_type': 'test-machine', 'operation': 'sar.train'})
            rows.append({'trajectory_id': name, 'strategy': 'random',
                         'request': str(request.relative_to(tmp_path))})
    with (tmp_path / 'trajectory-manifest.tsv').open('w') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter='\t')
        writer.writeheader()
        writer.writerows(rows)
    return argparse.Namespace(root=tmp_path, live=True, trajectory=None, job_group_id=None,
                              job_group_name='test-suite', project_id='test-project', job_name_suffix=None)


def test_injected_submission_uses_one_group_and_reuses_durable_receipts(tmp_path: Path) -> None:
    args = prepared(tmp_path)
    gateway = Gateway()
    assert _main_submit(args, gateway=gateway) == 0
    assert len(gateway.groups) == 1 and len(gateway.submissions) == 15
    assert all(job.parameters['job_group_id'] == 'group-test' for job in gateway.submissions)
    assert _main_submit(args, gateway=gateway) == 0
    assert len(gateway.groups) == 1 and len(gateway.submissions) == 15
    path = tmp_path / 'requests/autode-0.toml'
    request = load_toml(path)
    dump_toml(path, {**request, 'command': ['bash', 'changed.sh']})
    with pytest.raises(ValueError, match='Submitted request changed'):
        _main_submit(args, gateway=gateway)
    assert len(gateway.submissions) == 15


def test_unknown_submission_outcome_remains_blocked_on_restart(tmp_path: Path) -> None:
    args = prepared(tmp_path)
    gateway = Gateway(fail_first=True)
    assert _main_submit(args, gateway=gateway) == 1
    assert (tmp_path / 'submissions/autode-0/submission-inflight.json').exists()
    count = len(gateway.submissions)
    with pytest.raises(ValueError, match='Unresolved submission'):
        _main_submit(args, gateway=gateway)
    assert len(gateway.submissions) == count


def test_submission_without_live_flag_never_calls_gateway(tmp_path: Path) -> None:
    args = prepared(tmp_path)
    args.live = False
    gateway = Gateway()
    with pytest.raises(ValueError, match='requires --live'):
        _main_submit(args, gateway=gateway)
    assert not gateway.groups and not gateway.submissions


@pytest.mark.parametrize('change', [
    'externalId', 'nested-id', 'missing-hash', 'empty-id', 'numeric-id',
    'wrong-group', 'wrong-status', 'extra-alias', 'missing-run-id',
])
def test_invalid_receipt_is_never_reused_or_resubmitted(tmp_path: Path, change: str) -> None:
    args = prepared(tmp_path)
    args.trajectory = ['autode-0']
    gateway = Gateway()
    assert _main_submit(args, gateway=gateway) == 0
    receipt = tmp_path / 'submissions/autode-0/run.toml'
    state = load_toml(receipt)
    if change == 'externalId':
        state['externalId'] = state.pop('external_id')
    elif change == 'nested-id':
        state['unrelated'] = {'external_id': state.pop('external_id')}
    elif change == 'missing-hash':
        del state['request_sha256']
    elif change == 'empty-id':
        state['external_id'] = ' '
    elif change == 'numeric-id':
        state['external_id'] = 12
    elif change == 'wrong-group':
        state['job_group_id'] = 'another-group'
    elif change == 'wrong-status':
        state['status'] = 'failed'
    elif change == 'extra-alias':
        state['externalId'] = 'another-job'
    elif change == 'missing-run-id':
        del state['run_id']
    dump_toml(receipt, state)
    saved = receipt.read_bytes()
    with pytest.raises(ValueError, match='Invalid submission receipt'):
        _main_submit(args, gateway=gateway)
    assert len(gateway.groups) == 1 and len(gateway.submissions) == 1
    assert receipt.read_bytes() == saved
