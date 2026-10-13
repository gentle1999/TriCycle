from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tricycle_sampling.replay import recover_review as recovery
from tricycle_sampling.replay.campaign import digest, read, save


class ReviewRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.seed = self.root / 'reviewed/seed-02'
        self.directory = self.seed / 'round-0034/decision'
        self.directory.mkdir(parents=True)
        self.state = {'phase': 'blocked', 'blocked_phase': 'agent', 'round': 34, 'revision': 1,
                      'selected_ids': ['existing'], 'new_ids': ['existing'], 'failures': 1}
        save(self.seed / 'state.json', self.state)
        save(self.seed.parent / 'experiment.json', {'decision_protocol': {'review': True}})
        save(self.directory / 'task.json', {'budget': 1})
        save(self.directory / 'response.json', {'expression': 'new'})
        save(self.directory / 'evidence-hashes.json', {})
        save(self.directory.parent / 'input-hashes.json', {})
        save(self.directory.parent / 'result/worker-report.json', {'outputs': {}})
        self.assessment = {'proposal_sha256': digest(self.directory / 'response.json'),
                           'unique_scores': 3, 'previous_rule_diagnostic': {'unique_scores': 1}}
        self.addCleanup(patch.stopall)
        patch.object(recovery, 'verified_plan', return_value={
            'experiments': [{'root': str(self.seed.parent), 'variant': 'reviewed'}]}).start()
        patch.object(recovery, 'diagnostic_review', return_value=self.assessment).start()
        patch.object(recovery, 'validate').start()
        self.runner = patch.object(recovery, '_run_agent_once').start()

    def review_result(self, *_: object, **__: object) -> Path:
        response = self.directory / 'review-response.json'
        save(response, {'expression': 'reviewed'})
        return response

    def test_default_is_read_only(self) -> None:
        before = sorted(self.root.rglob('*'))
        self.assertEqual(recovery.recover(self.directory)['status'], 'read-only diagnosis')
        self.runner.assert_not_called()
        self.assertEqual(before, sorted(self.root.rglob('*')))

    def test_single_review_preserves_selection_and_resumes_original_phase(self) -> None:
        self.runner.side_effect = self.review_result
        recovery.recover(self.directory, live=True)
        self.runner.assert_called_once_with(self.directory / 'task.json',
            {'decision_protocol': {'review': True}}, review_pass=True)
        state = read(self.seed / 'state.json')
        self.assertEqual(state['phase'], 'agent')
        self.assertEqual(state['selected_ids'], self.state['selected_ids'])
        self.assertEqual(state['new_ids'], self.state['new_ids'])
        self.assertEqual(read(self.directory / 'review-approved.json')['review_passes'], 1)

    def test_interrupted_review_reuses_owned_cached_call(self) -> None:
        self.runner.side_effect = RuntimeError('interrupted')
        with self.assertRaises(RuntimeError):
            recovery.recover(self.directory, live=True)
        self.assertEqual(read(self.seed / 'state.json'), self.state)
        self.runner.side_effect = self.review_result
        with patch.object(recovery, 'diagnostic_review') as diagnostic:
            recovery.recover(self.directory, live=True)
            diagnostic.assert_not_called()
        self.assertEqual(read(self.directory / 'proposal-response.json'), {'expression': 'new'})

    def test_rejects_changed_proposal_after_interruption(self) -> None:
        self.runner.side_effect = RuntimeError('interrupted')
        with self.assertRaises(RuntimeError):
            recovery.recover(self.directory, live=True)
        save(self.directory / 'response.json', {'expression': 'foreign'})
        self.runner.reset_mock()
        with self.assertRaisesRegex(ValueError, 'active response changed'):
            recovery.recover(self.directory, live=True)
        self.runner.assert_not_called()

    def test_rejects_unowned_review(self) -> None:
        save(self.directory / 'review-agent-inflight.json', {})
        with self.assertRaisesRegex(ValueError, 'unowned review'):
            recovery.recover(self.directory, live=True)
        self.runner.assert_not_called()

    def test_invalid_review_leaves_trajectory_blocked(self) -> None:
        self.runner.side_effect = self.review_result
        with patch.object(recovery, 'validate', side_effect=[None, ValueError('bad review')]), \
                self.assertRaisesRegex(ValueError, 'bad review'):
            recovery.recover(self.directory, live=True)
        self.assertEqual(read(self.seed / 'state.json'), self.state)
        self.assertFalse((self.directory / 'review-approved.json').exists())

    def test_old_diagnostic_uses_full_pool_new_proposal_uses_real_budget(self) -> None:
        payload = {'mapped_reaction_smiles': 'C>>C', 'feature': 1}
        save(self.directory / 'pair-candidates.json', {'candidates': [
            {'candidate_id': str(i), 'payload': payload} for i in range(3)]})
        save(self.directory / 'acquisition-history.json', {'previous_rounds': [
            {'function': {'expression': 'old', 'cost_expression': 'cost'}}]})
        strategy = Mock()
        strategy.plan_with_context.side_effect = [
            {'candidate_scores': [{'candidate_id': str(i), 'score': i} for i in range(3)]},
            {'candidate_scores': [{'candidate_id': str(i), 'score': 0} for i in range(3)]}]
        # Exercise the real diagnostic independently of the recovery mocks.
        patch.stopall()
        with patch.object(recovery, 'AgentAuthoredAcquisitionSamplingStrategy', return_value=strategy), \
                patch.object(recovery, 'decision_spec', return_value={}), \
                patch.object(recovery.replay_mae, 'FIELDS', ('expression',)), \
                patch.object(recovery.replay_mae, 'region', return_value='region'), \
                patch.object(recovery.replay_mae, 'batch_similarity', return_value={}):
            result = recovery.diagnostic_review(self.directory)
        self.assertEqual([call.args[1] for call in strategy.plan_with_context.call_args_list], [1, 3])
        self.assertEqual(result['unique_scores'], 3)
        self.assertEqual(result['previous_rule_diagnostic']['unique_scores'], 1)
        self.assertEqual(result['previous_rule_on_current_pool_overlap'], 0)
        strategy.reset_mock()
        strategy.plan_with_context.side_effect = ValueError('invalid new proposal')
        with patch.object(recovery, 'AgentAuthoredAcquisitionSamplingStrategy', return_value=strategy), \
                patch.object(recovery, 'decision_spec', return_value={}), \
                self.assertRaisesRegex(ValueError, 'invalid new proposal'):
            recovery.diagnostic_review(self.directory)
        self.assertEqual(strategy.plan_with_context.call_count, 1)


if __name__ == '__main__':
    unittest.main()
