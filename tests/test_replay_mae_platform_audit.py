from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tricycle_sampling.replay import platform_audit as platform
from tricycle_sampling.replay.audit import reported_training_device
from tricycle_sampling.replay.campaign import save


class PlatformAuditTests(unittest.TestCase):
    def test_actual_device_is_read_from_training_result_not_warning_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'train.log'
            self.assertIsNone(reported_training_device(path))
            path.write_text('CUDA driver too old; device auto\n')
            self.assertIsNone(reported_training_device(path))
            path.write_text('CUDA driver warning\n{"device": "cpu", "refit": {}}\n')
            self.assertEqual(reported_training_device(path), 'cpu')
            path.write_text('{"device": "cuda", "refit": {}}\n')
            self.assertEqual(reported_training_device(path), 'cuda')

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        save(self.root / 'suite.json', {'maximum_sar_jobs': 1})
        save(self.root / 'shared-bohr/job-group.json', {'job_group_id': 'group'})
        save(self.root / 'autonomous/seed-00/round-0000/submission.json', {
            'external_id': '1', 'job_group_id': 'group', 'job_name': 'canonical'})
        self.snapshot = self.root / 'listing.json'
        self.listing: dict[str, Any] = {'job_group_id': 'group', 'observed_at': 'now',
                        'pagination': {'page': 1, 'has_more': False, 'total': 1},
                        'jobs': [{'bohrId': 1, 'jobName': 'canonical', 'phase': 'completed'}]}
        self.patch = patch.object(platform, 'verified_plan', return_value={'maximum_sar_jobs': 1})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def run_audit(self, *, require_complete: bool = False) -> dict[str, Any]:
        save(self.snapshot, self.listing)
        return platform.audit(self.root, self.root / 'output', snapshot=self.snapshot,
                              require_complete=require_complete)

    def test_complete_snapshot_binds_canonical_submission_without_network(self) -> None:
        result = self.run_audit(require_complete=True)
        self.assertTrue(result['complete'])
        self.assertFalse(result['failures'])
        self.assertEqual(len(result['submission_hashes']), 1)

    def test_partial_page_cannot_prove_coverage(self) -> None:
        self.listing['pagination']['has_more'] = True
        with self.assertRaisesRegex(ValueError, 'complete group coverage'):
            self.run_audit()
        self.assertFalse((self.root / 'output').exists())

    def test_duplicate_task_name_is_not_a_unique_canonical_job(self) -> None:
        self.listing['jobs'].append({'bohrId': 2, 'jobName': 'canonical', 'phase': 'completed'})
        self.listing['pagination']['total'] = 2
        result = self.run_audit(require_complete=True)
        self.assertFalse(result['complete'])
        self.assertIn('multiple platform jobs', result['failures'][0])

    def test_unknown_unclaimed_state_does_not_prove_termination(self) -> None:
        self.listing['jobs'].append({'bohrId': 2, 'jobName': 'unclaimed', 'phase': 'unknown'})
        self.listing['pagination']['total'] = 2
        result = self.run_audit(require_complete=True)
        self.assertFalse(result['complete'])
        self.assertTrue(result['failures'])

    def test_local_job_name_mismatch_fails_even_if_external_id_matches(self) -> None:
        self.listing['jobs'][0]['jobName'] = 'foreign'
        result = self.run_audit()
        self.assertFalse(result['complete'])
        self.assertIn('name differs', result['failures'][0])


if __name__ == '__main__':
    unittest.main()
