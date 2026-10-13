from __future__ import annotations

import fcntl
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from tricycle_sampling.replay import watch as watch
from tricycle_sampling.replay.campaign import digest, save


class SeedRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seed = self.root / "autonomous/seed-01"
        self.seed.mkdir(parents=True)
        self.state = {"seed": 1, "round": 17, "phase": "submit", "selected_ids": ["existing"], "new_ids": ["existing"]}
        save(self.seed / "state.json", self.state)
        self.record = self.root / "blocked.json"
        save(self.record, {**self.state, "phase": "blocked"})
        self.plan = {"experiments": [{"variant": "autonomous", "root": str(self.seed.parent)}]}
        self.addCleanup(patch.stopall)
        patch.object(watch, "verified_plan", return_value=self.plan).start()

    def test_dry_run_does_not_touch_gateway_or_controller(self) -> None:
        with patch.object(watch, "tick") as tick:
            self.assertEqual(watch.watch_seed(self.root, "autonomous", 1)["phase"], "submit")
            tick.assert_not_called()
        self.assertFalse((self.seed / ".recovery-supervisor.lock").exists())

    def test_recovery_binds_selection_and_prevents_a_second_watcher(self) -> None:
        save(self.record, {**self.state, "phase": "blocked", "new_ids": ["changed"]})
        with self.assertRaisesRegex(ValueError, "does not bind"):
            watch.watch_seed(self.root, "autonomous", 1, recovery_record=self.record, live=True)
        save(self.record, {**self.state, "phase": "blocked"})
        with (self.seed / ".recovery-supervisor.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                watch.watch_seed(self.root, "autonomous", 1, recovery_record=self.record, live=True)

    def test_uses_original_tick_and_stops_at_the_next_block(self) -> None:
        gateway = Mock(spec=watch.ReplayGateway)
        with patch.object(watch, "tick", return_value={**self.state, "phase": "blocked"}) as tick, \
                patch.object(watch, "update_reports"), patch.object(watch, "status"), \
                patch.object(watch.time, "sleep") as sleep:
            result = watch.watch_seed(self.root, "autonomous", 1, recovery_record=self.record,
                                      live=True, allow_submit=True, gateway=gateway)
            self.assertEqual(result["phase"], "blocked")
            tick.assert_called_once_with(self.seed.parent, self.seed, gateway, allow_submit=True)
            sleep.assert_not_called()

    def advanced_fixture(self, *, complete: bool = False) -> dict[str, Any]:
        previous = [str(i) for i in range(18)]
        archived = {**self.state, 'selected_ids': previous, 'new_ids': ['17'], 'phase': 'blocked'}
        save(self.record, archived)
        current = {**self.state, 'round': 18, 'selected_ids': previous + ['next'],
                   'new_ids': ['next'], 'phase': 'complete' if complete else 'monitor'}
        save(self.seed / 'state.json', current)
        save(self.seed.parent / 'experiment.json', {'batch_size': 1, 'round_count': 19 if complete else 20})
        save(self.seed / 'recovery-supervisor.json', {'recovery_record': str(self.record.resolve()),
                                                    'recovery_record_sha256': digest(self.record)})
        prior = self.seed / 'round-0016/result/checkpoint.pt'
        prior.parent.mkdir(parents=True)
        prior.write_bytes(b'previous')
        for index in range(17, 19 if complete else 18):
            directory = self.seed / f'round-{index:04d}'
            (directory / 'input').mkdir(parents=True)
            (directory / 'result').mkdir()
            own_previous = self.seed / f'round-{index-1:04d}' / 'result/checkpoint.pt'
            (directory / 'input/previous.pt').write_bytes(own_previous.read_bytes())
            save(directory / 'input/request.json', {'round_index': index,
                 'input_hashes': {'previous.pt': digest(directory / 'input/previous.pt')}})
            (directory / 'result/checkpoint.pt').write_bytes(f'checkpoint-{index}'.encode())
            save(directory / 'input-hashes.json', {n: digest(directory / 'input' / n)
                                                   for n in ('request.json', 'previous.pt')})
            save(directory / 'result/worker-report.json', {
                'request_sha256': digest(directory / 'input/request.json'),
                'outputs': {'checkpoint.pt': digest(directory / 'result/checkpoint.pt')}})
            save(directory / 'evaluation-summary.json', {'seed': 1, 'round_index': index,
                                                         'selected_ids': previous if index == 17 else current['selected_ids']})
            save(directory / 'reveal.json', {'new_ids': ['17'] if index == 17 else ['next']})
            if index == 17:
                save(directory / 'decision/selection.json', {'candidate_ids': ['next']})
        return current

    def test_restart_accepts_only_the_audited_owned_descendant(self) -> None:
        current = self.advanced_fixture()
        with patch('tricycle_sampling.replay.audit.audit_decision') as audit:
            watch.validate_recovery(self.seed, current, self.record)
            audit.assert_called_once_with(self.seed / 'round-0017/decision/selection.json')
            with self.subTest('changed selection'), \
                    self.assertRaisesRegex(ValueError, 'not the accepted descendant'):
                watch.validate_recovery(self.seed, {**current, 'new_ids': ['wrong']}, self.record)
            with self.subTest('changed checkpoint'):
                path = self.seed / 'round-0017/input/previous.pt'
                path.write_bytes(b'foreign')
                manifest = self.seed / 'round-0017/input-hashes.json'
                values = watch.read(manifest)
                values['previous.pt'] = digest(path)
                request = path.parent / 'request.json'
                request_value = watch.read(request)
                request_value['input_hashes']['previous.pt'] = digest(path)
                save(request, request_value)
                values['request.json'] = digest(request)
                save(manifest, values)
                worker_path = path.parent.parent / 'result/worker-report.json'
                worker = watch.read(worker_path)
                worker['request_sha256'] = digest(request)
                save(worker_path, worker)
                with self.assertRaisesRegex(ValueError, 'different trajectory'):
                    watch.validate_recovery(self.seed, current, self.record)

    def test_advanced_live_start_uses_the_frozen_controller(self) -> None:
        current = self.advanced_fixture()
        gateway = Mock(spec=watch.ReplayGateway)
        with patch('tricycle_sampling.replay.audit.audit_decision'), \
                patch.object(watch, 'tick', return_value=current) as tick, \
                patch.object(watch, 'update_reports'), patch.object(watch, 'status'):
            watch.watch_seed(self.root, 'autonomous', 1, recovery_record=self.record,
                             live=True, allow_submit=True, once=True, gateway=gateway)
            tick.assert_called_once_with(self.seed.parent, self.seed, gateway, allow_submit=True)
        self.assertEqual(watch.read(self.seed / 'recovery-supervisor.json')['recovery_record_sha256'],
                         digest(self.record))

    def test_advanced_restart_rejects_missing_or_foreign_owner(self) -> None:
        current = self.advanced_fixture()
        owner = self.seed / 'recovery-supervisor.json'
        owner.unlink()
        with self.assertRaisesRegex(ValueError, 'original supervisor'):
            watch.validate_recovery(self.seed, current, self.record)
        save(owner, {'recovery_record': str(self.root / 'other.json')})
        with self.assertRaisesRegex(ValueError, 'different archived operation'):
            watch.validate_recovery(self.seed, current, self.record)

    def test_completed_restart_verifies_ancestry_without_remote_work(self) -> None:
        self.advanced_fixture(complete=True)
        with patch('tricycle_sampling.replay.audit.audit_decision'), patch.object(watch, 'tick') as tick:
            self.assertEqual(watch.watch_seed(self.root, 'autonomous', 1, recovery_record=self.record,
                                             live=True, allow_submit=True)['phase'], 'complete')
            tick.assert_not_called()

    def test_premature_completion_is_rejected(self) -> None:
        current = self.advanced_fixture()
        with self.assertRaisesRegex(ValueError, 'before the final frozen round'):
            watch.validate_recovery(self.seed, {**current, 'phase': 'complete'}, self.record)

    def test_unchanged_round_cannot_claim_premature_completion(self) -> None:
        save(self.seed.parent / 'experiment.json', {'batch_size': 1, 'round_count': 20})
        with self.assertRaisesRegex(ValueError, 'before the final frozen round'):
            watch.validate_recovery(self.seed, {**self.state, 'phase': 'complete'}, self.record)

    def test_final_round_recovery_verifies_outputs_without_new_work(self) -> None:
        current = self.advanced_fixture(complete=True)
        save(self.record, {**current, 'phase': 'blocked'})
        save(self.seed / 'recovery-supervisor.json', {'recovery_record': str(self.record.resolve()),
                                                    'recovery_record_sha256': digest(self.record)})
        with patch('tricycle_sampling.replay.audit.audit_decision') as audit, patch.object(watch, 'tick') as tick:
            self.assertEqual(watch.watch_seed(self.root, 'autonomous', 1, recovery_record=self.record,
                                             live=True, allow_submit=True)['phase'], 'complete')
            audit.assert_not_called()
            tick.assert_not_called()


if __name__ == "__main__":
    unittest.main()
