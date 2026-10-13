from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from tricycle_sampling.replay import recover_retain as recovery
from tricycle_sampling.replay.campaign import digest, read, save


class RetainRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.seed = self.root / 'reviewed/seed-03'
        self.directory = self.seed / 'round-0037/decision'
        self.directory.mkdir(parents=True)
        self.raw = {key: 'text' for key in recovery.replay_mae.FIELDS}
        self.raw.update(expression='x', cost_expression='c', decision_action='retain')
        self.previous = {**self.raw, 'expression': 'x + 0'}
        save(self.directory / 'task.json', {'budget': 1, 'decision_protocol': {'review': True}})
        save(self.directory / 'acquisition-history.json', {'previous_rounds': [{'function': self.previous}]})
        save(self.directory / 'pair-candidates.json', {'candidates': [
            {'candidate_id': 'a', 'payload': {}}, {'candidate_id': 'b', 'payload': {}}]})
        save(self.directory / 'evidence-hashes.json', {})
        save(self.directory.parent / 'input-hashes.json', {})
        save(self.directory.parent / 'result/worker-report.json', {'outputs': {}})
        self.state = {'phase': 'blocked', 'blocked_phase': 'agent', 'round': 37,
                      'revision': 1, 'selected_ids': ['old'], 'new_ids': ['old']}
        save(self.seed / 'state.json', self.state)
        save(self.seed.parent / 'experiment.json', {'max_decision_attempts': 3,
                                                   'decision_protocol': {'review': True}})
        self.feedback = {'rejected_attempts': 3}
        save(self.directory / 'correction-feedback.json', self.feedback)
        rejected = self.directory / 'rejected-attempts/0003'
        save(rejected / 'response.pending.json', self.raw)
        save(rejected / 'rejection.json', {'error': 'retain must preserve the previous expression exactly',
                                          'task_sha256': digest(self.directory / 'task.json')})
        save(rejected / 'agent-exit.json', {'returncode': 0})
        save(rejected / 'agent-inflight.json', {'task_sha256': digest(self.directory / 'task.json')})
        self.addCleanup(patch.stopall)
        patch.object(recovery, 'verified_plan', return_value={}).start()
        patch.object(recovery, 'check_hashes').start()
        patch.object(recovery, 'validate').start()
        patch.object(recovery, 'decision_spec', side_effect=lambda _, value: value).start()
        self.strategy = Mock()
        self.rows: list[dict[str, Any]] = [{'candidate_id': 'a', 'score': 1, 'estimated_cost': 2},
                     {'candidate_id': 'b', 'score': 2, 'estimated_cost': 3}]
        self.strategy.plan_with_context.return_value = {'candidate_scores': self.rows}
        patch.object(recovery, 'AgentAuthoredAcquisitionSamplingStrategy', return_value=self.strategy).start()
        patch.object(recovery.replay_mae, 'review').start()
        self.runner = patch.object(recovery, '_run_agent_once').start()

    def test_exact_equivalence_restores_declared_retained_formula(self) -> None:
        canonical, proof = recovery.canonical_retain(self.directory, self.raw)
        self.assertEqual(canonical['expression'], self.previous['expression'])
        self.assertEqual(canonical['decision_action'], 'retain')
        self.assertEqual(proof['selected_ids'], ['b'])
        self.assertEqual(canonical['decision_source'], 'agent-retain-canonicalization')

    def test_scientific_score_or_cost_changes_cannot_be_reconciled(self) -> None:
        for field in ('score', 'estimated_cost'):
            with self.subTest(field=field):
                changed = [{**r, field: r[field]+1} for r in self.rows]
                self.strategy.plan_with_context.side_effect = [
                    {'candidate_scores': self.rows}, {'candidate_scores': changed}]
                with self.assertRaisesRegex(ValueError, 'changes scores or costs'):
                    recovery.canonical_retain(self.directory, self.raw)

    def test_nonretain_is_not_rewritten(self) -> None:
        with self.assertRaisesRegex(ValueError, 'explicit retain'):
            recovery.canonical_retain(self.directory, {**self.raw, 'decision_action': 'adjust'})

    def test_default_diagnosis_does_not_call_model_or_write_files(self) -> None:
        before = sorted(self.root.rglob('*'))
        self.assertEqual(recovery.recover(self.directory)['new_proposal_calls'], 0)
        self.runner.assert_not_called()
        self.assertEqual(before, sorted(self.root.rglob('*')))

    def review_result(self, *_: object, **__: object) -> Path:
        path = self.directory / 'review-response.json'
        save(path, read(self.directory / 'response.json'))
        return path

    def test_one_review_preserves_exhausted_counter_and_existing_selection(self) -> None:
        self.runner.side_effect = self.review_result
        recovery.recover(self.directory, live=True)
        self.assertEqual(self.runner.call_count, 1)
        self.assertTrue(self.runner.call_args.kwargs['review_pass'])
        self.assertEqual(read(self.directory / 'correction-feedback.json'), self.feedback)
        state = read(self.seed / 'state.json')
        self.assertEqual(state['phase'], 'select')
        self.assertEqual(state['selected_ids'], self.state['selected_ids'])
        self.assertEqual(read(self.directory / 'review-approved.json')['review_passes'], 1)

    def test_review_shorthand_is_reconciled_without_another_call(self) -> None:
        def shorthand(*_: object, **__: object) -> Path:
            save(self.directory / 'review-response.pending.json', self.raw)
            save(self.directory / 'review-agent-exit.json', {'returncode': 0})
            save(self.directory / 'review-agent-inflight.json', {
                 'task_sha256': digest(self.directory / 'task.json')})
            raise recovery.DecisionOutputError('retain must preserve the previous expression exactly')
        self.runner.side_effect = shorthand
        recovery.recover(self.directory, live=True)
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(read(self.directory / 'response.json')['expression'], self.previous['expression'])

    def test_invalid_review_keeps_blocked_state(self) -> None:
        self.runner.side_effect = recovery.DecisionOutputError('unrelated scientific error')
        with self.assertRaisesRegex(recovery.DecisionOutputError, 'unrelated'):
            recovery.recover(self.directory, live=True)
        self.assertEqual(read(self.seed / 'state.json'), self.state)
        self.assertFalse((self.directory / 'review-approved.json').exists())


if __name__ == '__main__':
    unittest.main()
