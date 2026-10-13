from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tricycle_core.io import file_sha256 as digest
from tricycle_core.io import write_json as save
from tricycle_sampling.replay import core_hours as core
from tricycle_sampling.replay import mae as replay_mae
from tricycle_sampling.replay import suite as replay_mae_suite

from tests.test_replay_mae import MaeWorkerTests


class CoreHourTests(unittest.TestCase):
    def test_budget_contract_rejects_invalid_units_and_ranges(self) -> None:
        for value in (None, [], [False], [0], [-1], [float('nan')], [float('inf')], [20, 10], [10, 10], ['10']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.budgets(value)
        self.assertEqual(core.budgets([50, 75]), [50., 75.])
        protocol = replay_mae.settings({'decision_protocol': {'name': core.NAME,
                                       'evaluation_budgets_core_hours': [50, 75]}})
        assert protocol is not None
        self.assertEqual(protocol['objective'], core.OBJECTIVE)

    def test_calibration_uses_only_exact_revealed_set_and_excludes_imputed_costs(self) -> None:
        observations = [{'candidate_id': 'a', 'realized_core_hours': 20, 'structural_cost_proxy': 10},
                        {'candidate_id': 'b', 'realized_core_hours': 1000, 'structural_cost_proxy': 10,
                         'cost_was_missing': True}]
        candidates: list[dict[str, Any]] = [{'candidate_id': 'c', 'payload': {'product_count': 2, 'mean_product_atom_count': 5}}]
        evidence = core.cost_evidence(observations, candidates, ['a', 'b'], 1020, [2000])
        self.assertEqual(candidates[0]['payload']['estimated_core_hours'], 20)
        self.assertEqual(evidence['calibration']['measured_pairs'], 1)
        self.assertEqual(evidence['calibration']['missing_cost_pairs'], 1)
        self.assertEqual(evidence['remaining_to_next_budget_core_hours'], 980)
        with self.assertRaisesRegex(ValueError, 'exactly'):
            core.cost_evidence(observations, candidates, ['a'], 20, [50])
        with self.assertRaisesRegex(ValueError, 'overlap'):
            core.cost_evidence(observations, [{'candidate_id': 'a', 'payload': {}}], ['a', 'b'], 1020, [2000])
        with self.assertRaisesRegex(ValueError, 'total'):
            core.cost_evidence(observations, candidates, ['a', 'b'], 999, [2000])

    def test_objective_has_resource_context_and_cost_units_without_forcing_score(self) -> None:
        task = {'objective': core.OBJECTIVE, 'decision_protocol': {'name': core.NAME}}
        value = {'expression': 'epistemic_variance', 'cost_expression': 'estimated_core_hours',
                 'rationale': '预算内探索。', 'author_notes': '成本可用于取舍，不固定惩罚。',
                 'decision_action': 'replace', 'hypothesis': 'Improve accuracy per core-hour.',
                 'expected_effect': 'Lower ground-truth MAE within the resource budget.'}
        response = replay_mae.compile_response(value, task, None)
        self.assertEqual(response['objective'], core.OBJECTIVE)
        self.assertEqual(response['expression'], 'epistemic_variance')
        with self.assertRaisesRegex(ValueError, 'units'):
            replay_mae.compile_response({**value, 'cost_expression': 'product_count * mean_product_atom_count'}, task, None)
        with self.assertRaisesRegex(ValueError, 'objective'):
            replay_mae.compile_response(value, {**task, 'objective': replay_mae.OBJECTIVE}, None)

    def test_prompt_keeps_resource_objective_and_surrogate_boundary(self) -> None:
        brief = {'objective': core.OBJECTIVE, 'decision_protocol': {'name': core.NAME},
                 'core_hour_budget': {'unit': 'core_hours', 'spent_core_hours': 100}}
        prompt = replay_mae.prompt(brief)
        self.assertIn('CUMULATIVE PROXY CORE-HOUR BUDGETS', prompt)
        self.assertNotIn('at the supplied sample budget', prompt)
        self.assertNotIn('NOT a secondary optimization target', prompt)
        self.assertIn('Ground-truth labels and metrics are unavailable', prompt)
        self.assertIn('does not divide scores by cost', prompt)

    def test_tie_diagnostics_reveal_id_effect_without_changing_selection(self) -> None:
        ranking = [{'candidate_id': x, 'score': 1.} for x in ('a+1', 'a+2', 'b+3')]
        candidates = [{'candidate_id': r['candidate_id'], 'payload': {'estimated_core_hours': 5.}}
                      for r in ranking]
        before = copy.deepcopy(ranking)
        result = core.ranking_diagnostics(ranking, 2, candidates)
        self.assertEqual(result['cutoff_tied_candidates'], 3)
        self.assertEqual(result['selected_unique_ene'], 1)
        self.assertEqual(result['estimated_batch_core_hours'], 10)
        self.assertEqual(ranking, before)

    def points(self) -> list[dict[str, Any]]:
        return [{'variant': v, 'seed': s, 'round_index': r, 'ground_truth_sha256': 'gt',
                 'n': 3021, 'unit': 'kcal/mol', 'role': 'evaluation-only',
                 'cumulative_sampling_cost_core_hours': cost, 'mae': mae, 'rmse': mae+1}
                for v in ('autonomous', 'feedback', 'reviewed') for s in range(5)
                for r, (cost, mae) in enumerate(((10, 5), (20, 6), (30, 3)))]

    def test_budget_metric_uses_latest_affordable_not_best_or_over_budget(self) -> None:
        report = core.aggregate(self.points(), ['autonomous', 'feedback', 'reviewed'], [25])
        self.assertEqual(report['budget_axis'], 'core_hours')
        self.assertEqual(len(report['summaries']), 3)
        self.assertTrue(all(r['mae_mean'] == 6 for r in report['summaries']))
        self.assertEqual(len(report['paired_differences']), 2)
        self.assertNotIn('sampled_pairs', json.dumps(report))

    def test_unfinished_seed_does_not_change_cohort_or_extrapolate(self) -> None:
        points = [p for p in self.points() if not (p['variant'] == 'feedback' and p['seed'] == 4 and p['round_index'] == 2)]
        report = core.aggregate(points, ['autonomous', 'feedback', 'reviewed'], [25])
        self.assertEqual({r['variant'] for r in report['summaries']}, {'autonomous', 'reviewed'})

    def test_suite_report_uses_core_hours_even_if_sample_counts_differ(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            experiments = []
            for v in ('autonomous', 'feedback', 'reviewed'):
                directory = root / v
                save(directory / 'experiment.json', {'controller_hashes': {}})
                for s in range(5):
                    save(directory / f'seed-{s:02d}/state.json', {'seed': s, 'phase': 'complete',
                         'round': 2, 'selected_ids': [v]})
                    for p in (x for x in self.points() if x['variant'] == v and x['seed'] == s):
                        save(directory / f"seed-{s:02d}/round-{p['round_index']:04d}/evaluation-summary.json",
                             {**p, 'selected_ids': [v]})
                experiments.append({'variant': v, 'root': str(directory),
                                    'config_sha256': digest(directory / 'experiment.json')})
            save(root / 'suite.json', {'suite_id': 'core-test', 'objective': core.OBJECTIVE,
                 'budget_axis': 'core_hours', 'evaluation_budgets_core_hours': [25], 'experiments': experiments})
            save(root / 'suite-hash.json', {'sha256': digest(root / 'suite.json')})
            replay_mae_suite.status(root)
            report = (root / 'REPORT.md').read_text()
            self.assertIn('核时预算', report)
            self.assertNotIn('匹配样本量', report)
            self.assertNotIn('样本对数', report)


class CoreHourWorkerTests(MaeWorkerTests):
    protocol_name = core.NAME


if __name__ == '__main__':
    unittest.main()
