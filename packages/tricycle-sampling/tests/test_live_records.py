"""Crash recovery and controller invariants for native trajectory containers."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from tricycle_core import record_io as rio
from tricycle_core.record_ledger import RecordLedger
from tricycle_sampling.replay import campaign
from tricycle_sampling.replay.agent_records import invoke, recover_finished
from tricycle_sampling.replay.decision import DecisionTimeoutError, archive_timeout
from tricycle_sampling.replay.runtime_records import initialize, migrate, work_directory


@pytest.fixture
def seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv('TRICYCLE_RECORDS_ROOT', str(tmp_path / 'local'))
    path = tmp_path / 'experiment/seed-00'
    path.mkdir(parents=True)
    initialize(path)
    return path


def ledger(seed: Path) -> RecordLedger:
    found = rio.location(seed)
    assert found is not None
    return found[0]


def test_records_never_expand_and_assets_remain_references(seed: Path, tmp_path: Path) -> None:
    shared = tmp_path / 'shared'
    shared.mkdir()
    (shared / 'prediction.csv').write_bytes(b'unchanged\n')
    for index in range(30):
        rio.write_json(seed / 'state.json', {'round': index})
        rio.write_json(seed / f'round-{index:04d}/decision/response.json', {'round': index})
    rio.symlink_to(seed / 'result', shared)
    assert rio.read_bytes(seed / 'result/prediction.csv') == b'unchanged\n'
    with pytest.raises(ValueError, match='shared asset'):
        rio.write_bytes(seed / 'result/prediction.csv', b'wrong')
    assert len(list(rio.glob(seed, 'round-*/decision/response.json'))) == 30
    assert {p.name for p in seed.iterdir()} == {'records.sqlite'}
    assert campaign.read(seed / 'state.json') == {'round': 29}
    ledger(seed).verify()


def test_transactions_roll_back_state_and_audit_together(seed: Path) -> None:
    rio.write_json(seed / 'state.json', {'phase': 'submit'})
    before = ledger(seed).verify()
    with pytest.raises(RuntimeError), rio.transaction(seed):
        rio.write_json(seed / 'state.json', {'phase': 'monitor'})
        rio.write_json(seed / 'submission.json', {'external_id': 'remote-42'})
        raise RuntimeError('process failed before commit')
    assert ledger(seed).verify() == before
    assert campaign.read(seed / 'state.json')['phase'] == 'submit'
    assert not rio.exists(seed / 'submission.json')


def test_process_exit_preserves_committed_intent_not_partial_transaction(seed: Path) -> None:
    script = '''
import json
import os
from pathlib import Path
from tricycle_core.record_ledger import RecordLedger
store=RecordLedger(Path(os.environ['TEST_LEDGER']))
store.put('submission-inflight.json', b'{"job_name":"stable"}')
with store.transaction():
    store.put('submission.json', b'{"external_id":"42"}')
    os._exit(71)
'''
    result = subprocess.run([sys.executable, '-c', script], env={**os.environ,
        'PYTHONPATH': os.pathsep.join(sys.path), 'TEST_LEDGER': str(ledger(seed).database)}, check=False)
    assert result.returncode == 71
    assert campaign.read(seed / 'submission-inflight.json')['job_name'] == 'stable'
    assert not rio.exists(seed / 'submission.json')
    ledger(seed).verify()


def test_concurrent_append_backup_and_tamper_detection(seed: Path, tmp_path: Path) -> None:
    def append(index: int) -> None:
        for _ in range(8):
            rio.append_bytes(seed / 'agent.log', f'{index}\n'.encode())
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(4)))
    assert len(rio.read_text(seed / 'agent.log').splitlines()) == 32
    output = tmp_path / 'snapshot.sqlite'
    ledger(seed).backup(output)
    assert RecordLedger(output).read('agent.log') == rio.read_bytes(seed / 'agent.log')
    with pytest.raises(FileExistsError):
        ledger(seed).backup(output)
    with sqlite3.connect(output) as connection:
        connection.execute("UPDATE events SET sha256='altered' WHERE sequence=1")
    with pytest.raises(ValueError, match='audit chain'):
        RecordLedger(output).verify()


@pytest.mark.parametrize('key', ['.', '../secret', '/secret', '.env', '.venv/file', '__pycache__/x.pyc'])
def test_runtime_and_unsafe_records_rejected(seed: Path, key: str) -> None:
    with pytest.raises(ValueError):
        ledger(seed).put(key, b'forbidden')


def test_active_migration_preserves_original_evidence(tmp_path: Path) -> None:
    seed = tmp_path / 'seed-00'
    (seed / 'round-0002/rejected-attempts/0001').mkdir(parents=True)
    payloads = {'state.json': b'{"round":2,"phase":"monitor"}\n',
        'round-0002/rejected-attempts/0001/response.json': '{"rationale":"原始记录"}\n'.encode(),
        'round-0002/submission.json': b'{"external_id":"original"}'}
    for key, value in payloads.items():
        (seed / key).write_bytes(value)
    migrate(seed, storage_root=tmp_path / 'local')
    for key, value in payloads.items():
        assert rio.read_bytes(seed / key) == value
    assert {p.name for p in seed.iterdir()} == {'records.sqlite', '.lock'}
    # Repeating after publication is safe and keeps the original evidence backup.
    migrate(seed, storage_root=tmp_path / 'local')
    assert len(list((tmp_path / 'local').glob('*.migration.sqlite'))) == 1
    snapshot = next((tmp_path / 'local').glob('*.migration.sqlite'))
    assert RecordLedger(snapshot).read('state.json') == payloads['state.json']


def test_saved_remote_receipt_prevents_duplicate_submission(seed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    campaign.save(seed / 'state.json', {'seed': 0, 'round': 0, 'revision': 0, 'phase': 'submit', 'failures': 0})
    campaign.save(seed.parent / 'experiment.json', {})
    campaign.save(seed.parent / 'experiment-hash.json', {'sha256': campaign.digest(seed.parent / 'experiment.json')})
    campaign.save(seed / 'round-0000/submission-inflight.json', {'job_name': 'stable'})
    campaign.save(seed / 'round-0000/submission.json', {'external_id': 'original-42'})
    monkeypatch.setattr(campaign, 'verify_inputs', lambda _: None)
    gateway = Mock()
    state = campaign.tick(seed.parent, seed, gateway, allow_submit=True)
    assert state['phase'] == 'monitor'
    assert state['external_id'] == 'original-42'
    gateway.submit.assert_not_called()
    ledger(seed).verify()


def test_unknown_submit_is_blocked_without_resubmit(seed: Path) -> None:
    current = seed / 'round-0000'
    campaign.save(current / 'submission-inflight.json', {'job_name': 'stable'})
    gateway = Mock()
    with pytest.raises(ValueError, match='never resubmit blindly'):
        campaign.reconcile_timeout_submission(current, gateway)
    gateway.submit.assert_not_called()
    assert rio.exists(current / 'submission-inflight.json')


def agent_inputs(seed: Path) -> tuple[Path, list[str]]:
    directory = seed / 'round-0000/decision'
    rio.write_json(directory / 'task.json', {'round': 0})
    rio.write_json(directory / 'response-schema.json', {})
    command = [sys.executable, '-c', '''
import pathlib,sys
print('English decision progress', flush=True)
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('{"result":"complete"}')
''', '--cd', 'logical', '--output-schema', 'logical', '--output-last-message', 'logical']
    return directory, command


def test_agent_output_and_logs_go_directly_to_container(seed: Path) -> None:
    directory, command = agent_inputs(seed)
    invoke(directory, '', command, 'Decide in English.', timeout=10, task_sha256=rio.file_sha256(directory / 'task.json'))
    assert 'English decision progress' in rio.read_text(directory / 'agent.log')
    assert campaign.read(directory / 'response.pending.json')['result'] == 'complete'
    assert not directory.exists()
    assert campaign.read(directory / 'agent-exit.json')['returncode'] == 0
    ledger(seed).verify()


def test_agent_timeout_retains_logs_and_restart_opportunity(seed: Path) -> None:
    directory, command = agent_inputs(seed)
    command[2] = "import time; print('started', flush=True); time.sleep(30)"
    with pytest.raises(DecisionTimeoutError) as error:
        invoke(directory, '', command, 'Decide in English.', timeout=1, task_sha256=rio.file_sha256(directory / 'task.json'))
    assert archive_timeout(directory, error.value)
    archive = directory / 'timeout-attempts/proposal/0001'
    assert rio.read_text(archive / 'agent.log') == 'started\n'
    assert not rio.exists(directory / 'agent-inflight.json')
    assert not rio.exists(directory / 'correction-feedback.json')


def test_finished_interrupted_agent_imports_without_rerun(seed: Path) -> None:
    directory, _ = agent_inputs(seed)
    workspace = work_directory(directory.parent, 'agent')
    pending = workspace / 'response.pending.json'
    pending.write_bytes(b'{"result":"already finished"}\n')
    rio.write_json(directory / 'agent-inflight.json', {'task_sha256': rio.file_sha256(directory / 'task.json'), 'work_directory': str(workspace)})
    dead = subprocess.Popen([sys.executable, '-c', 'pass'])
    dead.wait()
    rio.write_json(directory / 'agent-process.json', {'pid': dead.pid})
    recover_finished(directory)
    assert rio.read_bytes(directory / 'response.pending.json') == pending.read_bytes()
    assert campaign.read(directory / 'agent-exit.json')['returncode'] is None
    assert rio.exists(directory / 'agent-inflight.json')
    ledger(seed).verify()


def test_backup_includes_committed_wal_while_writer_remains_open(seed: Path, tmp_path: Path) -> None:
    with sqlite3.connect(ledger(seed).database) as keeper:
        keeper.execute('PRAGMA wal_autocheckpoint=0')
        keeper.execute('BEGIN')
        keeper.execute('SELECT * FROM metadata').fetchall()
        rio.write_json(seed / 'receipt.json', {'external_id': 'committed-in-wal'})
        assert ledger(seed).database.with_name(ledger(seed).database.name + '-wal').stat().st_size > 0
        output = tmp_path / 'wal-snapshot.sqlite'
        ledger(seed).backup(output)
        assert json.loads(RecordLedger(output).read('receipt.json'))['external_id'] == 'committed-in-wal'


def test_timeout_covers_blocked_prompt_delivery(seed: Path) -> None:
    directory, command = agent_inputs(seed)
    command[2] = 'import time; time.sleep(30)'
    with pytest.raises(DecisionTimeoutError):
        invoke(directory, '', command, 'x' * 2_000_000, timeout=1,
               task_sha256=rio.file_sha256(directory / 'task.json'))
    assert rio.exists(directory / 'agent-exit.json')


def test_state_journal_and_head_roll_back_as_one_transaction(seed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = {'seed': 0, 'round': 0, 'revision': 0, 'phase': 'submit'}
    campaign.save_state(seed, state)
    before = ledger(seed).verify()
    original = campaign.save
    def fail_head(path: Path, value: object) -> None:
        if path.name == 'state.json':
            raise OSError('simulated disk error')
        original(path, value)
    monkeypatch.setattr(campaign, 'save', fail_head)
    with pytest.raises(OSError):
        campaign.save_state(seed, {**state, 'phase': 'monitor'})
    assert ledger(seed).verify() == before
