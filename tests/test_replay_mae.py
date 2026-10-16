from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tricycle_core import ExecutionContext, InvalidRequestError
from tricycle_core.io import file_sha256 as digest
from tricycle_core.io import write_json as save
from tricycle_core.toml import dump_toml, load_toml
from tricycle_sampling import Candidate, SamplingPlugin, SamplingRequest
from tricycle_sampling.replay import audit as replay_mae_audit
from tricycle_sampling.replay import campaign as replay_campaign
from tricycle_sampling.replay import core_hours as replay_core_hours
from tricycle_sampling.replay import mae as replay_mae
from tricycle_sampling.replay import suite as replay_mae_suite
from tricycle_sampling.replay import worker as replay_sar_worker
from tricycle_sampling.replay.decision import DecisionOutputError, validate
from tricycle_sampling.replay.selection import select_round


class MaeSettingsTests(unittest.TestCase):
    def test_protocol_settings_round_trip_with_feedback_review_and_guidance(self) -> None:
        protocols: list[dict[str, Any]] = [
            {"name": replay_mae.NAME},
            {"name": replay_mae.NAME, "feedback": True},
            {"name": replay_mae.NAME, "feedback": True, "review": True},
            {"name": replay_core_hours.NAME, "evaluation_budgets_core_hours": [50, 100]},
            {"name": replay_core_hours.NAME, "evaluation_budgets_core_hours": [50, 100],
             "feedback": True, "review": True, "guidance": "coverage-cost-guided-v1"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "campaign.toml"
            for protocol in protocols:
                with self.subTest(protocol=protocol):
                    parsed = replay_mae.settings({"decision_protocol": protocol})
                    dump_toml(path, {"decision_protocol": parsed})
                    self.assertEqual(replay_mae.settings(load_toml(path)), parsed)
                    assert parsed is not None
                    self.assertEqual(parsed["review"], protocol.get("review", False))

    def test_protocol_typos_fail_before_campaign_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config, run = Path(temporary) / "request.toml", Path(temporary) / "run"
            for typo in ("reveiw", "feedbak"):
                with self.subTest(typo=typo):
                    dump_toml(config, {"decision_protocol": {"name": replay_mae.NAME, typo: True}})
                    with self.assertRaisesRegex(ValueError, f"unknown decision_protocol fields: {typo}"):
                        replay_campaign.prepare(config, run)
                    self.assertFalse(run.exists())

    def test_explicit_protocol_must_be_a_table(self) -> None:
        self.assertIsNone(replay_mae.settings({}))
        invalid: tuple[Any, ...] = (None, [], "autonomous-mae-v1", False)
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "must be a table"):
                replay_mae.settings({"decision_protocol": value})

    def test_protocol_cannot_discard_inapplicable_fields_or_override_objective(self) -> None:
        for extra in (
            {"evaluation_budgets_core_hours": [50]},
            {"guidance": "coverage-cost-guided-v1"},
            {"objective": replay_core_hours.OBJECTIVE},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                replay_mae.settings({"decision_protocol": {"name": replay_mae.NAME, **extra}})


class MaeDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'decision'
        self.directory.mkdir()
        download = self.root / 'download'
        download.mkdir()
        (download / 'result').write_text('complete')
        (download / 'download-manifest.txt').write_text('result\n')
        (self.root / 'status.tsv').write_text('worker\tterminal\tresult\nsar\tyes\tSUCCEEDED\n')
        (self.root / 'download-results.tsv').write_text(f'worker\tstatus\tdownload_dir\nsar\tDOWNLOADED\t{download}\n')
        save(self.root / 'reveal.json', {'selected_ids': []})
        self.context = {'sampled_distribution': {'novelty_definition': 'test', 'coverage_gap': .2},
                        'model_metrics': {'validation_mae': 2.}}
        self.protocol = replay_mae.settings({'decision_protocol': {'name': replay_mae.NAME}})
        save(self.directory / 'task.json', {'budget': 1, 'candidate_count': 2,
                                           'context': self.context, 'decision_protocol': self.protocol})
        self.candidates: list[dict[str, Any]] = [
            {'candidate_id': 'cheap', 'payload': {'epistemic_variance': 1., 'sampled_novelty': .5,
              'product_count': 1, 'mean_product_atom_count': 2., 'product_ring_count': 0.,
              'mapped_reaction_smiles': 'CC>>CC'}},
            {'candidate_id': 'expensive', 'payload': {'epistemic_variance': 2., 'sampled_novelty': .5,
              'product_count': 10, 'mean_product_atom_count': 30., 'product_ring_count': 3.,
              'mapped_reaction_smiles': 'CCC>>CCC'}},
        ]
        save(self.directory / 'pair-candidates.json', {'candidates': self.candidates})
        save(self.directory / 'model-metrics.json', {'mae': 2.})
        save(self.directory / 'acquisition-history.json', {'previous_rounds': []})
        self.response = {'expression': 'epistemic_variance',
                         'cost_expression': 'product_count * mean_product_atom_count',
                         'rationale': 'Prioritize validation in high-uncertainty regions.', 'author_notes': 'No cost penalty; evidence limited.',
                         'decision_action': 'replace', 'hypothesis': 'Uncertain region is undersampled.',
                         'expected_effect': 'Reduce regional validation error.',
                         'objective': replay_mae.OBJECTIVE,
                         'task_sha256': digest(self.directory / 'task.json')}

    def metadata(self) -> dict[str, Any]:
        return {'agent_acquisition': {
            **self.response, 'schema': replay_mae.SCHEMA, 'function_id': 'free', 'version': '1.0.0',
            'authored_by': 'agent-authored-acquisition', 'context': self.context,
            'evidence': {'sampled_set': 'sha256:selected', 'candidate_space': 'sha256:pool',
                         'model_evaluation': 'sha256:validation',
                         **{key: {'manifest_path': str(self.root / filename),
                                  'sha256': digest(self.root / filename)}
                            for key, filename in [('calculation_status', 'status.tsv'),
                                                  ('downloaded_results', 'download-results.tsv')]}}}}

    def test_mae_selection_can_ignore_cost_novelty_and_global_metrics(self) -> None:
        request = SamplingRequest(candidates=tuple(Candidate(**c) for c in self.candidates),
                                  budget=1, strategy='agent-authored-acquisition', metadata=self.metadata())
        plugin = SamplingPlugin()
        self.assertEqual(plugin.select(request).selected_ids, ('expensive',))
        definition = plugin.plan_select(request, ExecutionContext('test')).parameters['acquisition_function']
        self.assertEqual(definition['schema'], replay_mae.SCHEMA)
        self.assertEqual(definition['decision_action'], 'replace')
        self.assertEqual(definition['cost_summary']['total'], 302.)
        self.assertEqual(plugin.select(request).selected_ids, plugin.select(request).selected_ids)

    def test_round_commit_reuses_selection_and_rejects_request_or_artifact_drift(self) -> None:
        request = {
            'candidates': self.candidates, 'budget': 1, 'strategy': 'agent-authored-acquisition',
            'metadata': self.metadata(), 'output_dir': str(self.root / 'selection'), 'round_index': 0,
            'model_snapshot': {'snapshot_id': 'round-0', 'checkpoint_hash': 'checkpoint',
                               'score_artifact': 'predictions'},
            'observed_metrics': self.context['model_metrics'],
        }
        result = select_round(request)
        self.assertEqual(result['selected_ids'], ['expensive'])
        directory = self.root / 'selection/rounds/0000'
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        self.assertEqual(select_round(request), result)
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})
        with self.assertRaisesRegex(ValueError, 'differs from this request'):
            select_round({**request, 'seed': 23})
        with self.assertRaisesRegex(ValueError, 'model metrics differ'):
            select_round({**request, 'observed_metrics': {'validation_mae': 1.}})
        (directory / 'model-scores.tsv').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'artifact changed'):
            select_round(request)

    def test_legacy_constraints_and_integrity_barrier_remain(self) -> None:
        metadata = self.metadata()
        metadata['agent_acquisition']['schema'] = 'tricycle.agent-acquisition/v1'
        request = SamplingRequest(candidates=tuple(Candidate(**c) for c in self.candidates),
                                  budget=1, strategy='agent-authored-acquisition', metadata=metadata)
        with self.assertRaisesRegex(InvalidRequestError, 'estimated_cost'):
            SamplingPlugin().select(request)
        metadata = self.metadata()
        (self.root / 'status.tsv').write_text('worker\tterminal\tresult\nsar\tno\tRUNNING\n')
        request = SamplingRequest(candidates=tuple(Candidate(**c) for c in self.candidates),
                                  budget=1, strategy='agent-authored-acquisition', metadata=metadata)
        with self.assertRaises(InvalidRequestError):
            SamplingPlugin().select(request)

    def test_retain_is_explicit_and_preserves_the_previous_rule(self) -> None:
        task = replay_campaign.read(self.directory / 'task.json')
        value = {**self.response, 'decision_action': 'retain'}
        with self.assertRaisesRegex(ValueError, 'first decision'):
            replay_mae.compile_response(value, task, None)
        replay_mae.compile_response(value, task, self.response)
        with self.assertRaisesRegex(ValueError, 'preserve'):
            replay_mae.compile_response({**value, 'expression': 'sampled_novelty'}, task, self.response)

    def test_retry_prompt_distinguishes_accepted_rule_from_rejected_proposal(self) -> None:
        accepted = {**self.response, 'expression': 'sampled_novelty'}
        rejected = {**self.response, 'decision_action': 'retain'}
        save(self.directory / 'acquisition-history.json', {'previous_rounds': [{'function': accepted}]})
        save(self.directory / 'rejected-attempts/0001/response.pending.json', rejected)
        save(self.directory / 'correction-feedback.json', {
            'rejected_attempts': 1, 'error': 'retain must preserve the previous expression exactly',
            'previous_output_directory': 'rejected-attempts/0001'})
        brief = {'decision_protocol': self.protocol,
                 'recent_functions': [{'round': 0, **accepted}]}
        save(self.directory / 'decision-brief.json', brief)
        with patch.object(replay_campaign, 'build_decision_brief', return_value=brief), \
                patch.object(replay_campaign.subprocess, 'Popen') as popen:
            process = popen.return_value
            process.pid, process.returncode = 123, 0

            def respond(*_: object, **__: object) -> None:
                save(self.directory / 'response.pending.json', {**rejected, 'decision_action': 'adjust'})

            process.communicate.side_effect = respond
            response = replay_campaign._run_agent_once(self.directory / 'task.json', {'agent_command': ['fake']})
            prompt = process.communicate.call_args.args[0].decode()
        self.assertIn('Rejected proposal (NOT the last accepted round)', prompt)
        self.assertIn('AUTOMATIC VALIDATION REPAIR', prompt)
        self.assertIn('not to restart scientific strategy design', prompt)
        self.assertNotIn('\nPrevious formula:', prompt)
        material = json.loads(prompt.split('\n')[1])
        self.assertEqual(material['last_accepted_round_formula']['expression'], 'sampled_novelty')
        self.assertEqual(replay_campaign.read(response)['decision_action'], 'adjust')

    def test_nonfinite_unsafe_and_constant_rankings_are_rejected(self) -> None:
        for expression in ('1', '1 / 0', "__import__('os')"):
            with self.subTest(expression=expression), self.assertRaises(DecisionOutputError):
                validate(self.directory, {**self.response, 'expression': expression})

    def test_invalid_decision_is_repaired_automatically_before_returning(self) -> None:
        calls: list[int] = []
        task_path = self.directory / 'task.json'
        save(task_path, {**replay_campaign.read(task_path), 'round': 0})
        self.response['task_sha256'] = digest(task_path)

        def invoke(*_: object, **__: object) -> Path:
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                save(self.directory / 'response.pending.json', {**self.response, 'decision_action': 'retain'})
                raise DecisionOutputError('cannot retain a function before the first decision')
            feedback = replay_campaign.read(self.directory / 'correction-feedback.json')
            self.assertEqual(feedback['rejected_attempts'], 1)
            save(self.directory / 'response.json', self.response)
            return self.directory / 'response.json'

        with patch.object(replay_campaign, '_run_agent_once', side_effect=invoke):
            result = replay_campaign.run_agent(self.directory / 'task.json', {'max_decision_attempts': 3})
        self.assertEqual(len(calls), 2)
        self.assertEqual(replay_campaign.read(result)['decision_action'], 'replace')
        self.assertTrue((self.directory / 'rejected-attempts/0001/response.pending.json').exists())
        validate(self.directory, self.response)

    def test_batch_review_uses_no_evaluation_file(self) -> None:
        save(self.directory / 'response.json', self.response)
        save(self.root / 'evaluation-summary.json', {'mae': 99999})
        assessment = replay_mae.review(self.directory, self.response)
        self.assertEqual(assessment['selected_count'], 1)
        self.assertEqual(assessment['selected_examples'][0]['candidate_id'], 'expensive')
        self.assertNotIn('99999', json.dumps(assessment))
        self.assertEqual(replay_mae.batch_similarity(['CC>>CC', 'CC>>CC'])['mean'], 1.)

    def test_all_remaining_selection_skips_model_but_checks_evidence(self) -> None:
        task = replay_campaign.read(self.directory / 'task.json')
        task['budget'] = 2
        save(self.directory / 'task.json', task)
        save(self.directory / 'evidence-hashes.json', {'task.json': digest(self.directory / 'task.json')})
        with patch.object(replay_campaign, '_run_agent_once') as agent, patch.object(replay_campaign, 'build_decision_brief'):
            response_path = replay_campaign.run_agent(self.directory / 'task.json', {})
            agent.assert_not_called()
        response = replay_campaign.read(response_path)
        self.assertEqual(response['decision_source'], 'controller-all-remaining')
        validate(self.directory, response)
        (self.root / 'download' / 'result').unlink()
        with patch.object(replay_campaign, 'build_decision_brief'), self.assertRaises(InvalidRequestError):
            replay_campaign.run_agent(self.directory / 'task.json', {})

    def test_review_is_bounded_and_restart_reuses_approved_response(self) -> None:
        task = replay_campaign.read(self.directory / 'task.json')
        task['decision_protocol'] = {'name': replay_mae.NAME, 'feedback': True, 'review': True}
        save(self.directory / 'task.json', task)
        response = {**self.response, 'task_sha256': digest(self.directory / 'task.json')}
        save(self.directory / 'response.json', response)
        save(self.directory / 'review-response.json', response)
        with patch.object(replay_campaign, '_run_agent_once', side_effect=[self.directory / 'response.json', self.directory / 'review-response.json']) as agent:
            replay_campaign.run_agent(self.directory / 'task.json', {})
            self.assertEqual(agent.call_count, 2)
            self.assertTrue(agent.call_args_list[1].kwargs['review_pass'])
        with patch.object(replay_campaign.subprocess, 'Popen') as process:
            replay_campaign.run_agent(self.directory / 'task.json', {})
            process.assert_not_called()


class MaeWorkerTests(unittest.TestCase):
    protocol_guidance: str
    def test_fixed_panel_and_pre_update_predictions_use_selected_labels_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [{key: str(i) for key in replay_sar_worker.baseline.NATIVE_COLUMNS} for i in range(10)]
            for i, row in enumerate(rows):
                row.update({'rxn_smiles': 'CC>>CC', 'G_T_activate': str(i+1), 'prod_id': '0'})
            panel = [replay_sar_worker.pair(r) for r in rows[:2]]
            for name, data in [('training.csv', rows), ('newly-revealed.csv', rows[-2:]),
                               ('candidates.csv', rows[:1]), ('evaluation.csv', [{**rows[0], 'G_T_activate': '99999'}])]:
                replay_sar_worker.baseline._write_csv(root / name, list(replay_sar_worker.baseline.NATIVE_COLUMNS), data)
            from tricycle_sar.sampling_runtime import stage as stage_sar_runtime

            sar_backend = stage_sar_runtime(root)
            (root / 'previous.pt').write_bytes(b'previous main model')
            save(root / 'request.json', {'input_hashes': {}, 'round_index': 1,
                                        'training': {'epochs': 300, 'batch_size_paths': 512, 'device': 'cpu'},
                                        'source_revision': sar_backend['source_revision'], 'sar_backend': sar_backend,
                                        'evaluation_sha256': 'evaluation-only',
                                        'reuse_checkpoint': False, 'need_decision': True,
                                        'validation_pair_ids': panel, 'mae_protocol': {'name': replay_mae.NAME}})
            calls: list[tuple[str, str, Any]] = []

            def fake_fit(data: Path, out: Path, recipe: dict[str, Any], previous: Path | None) -> Path:
                calls.append(('fit', data.name, previous))
                out.mkdir(parents=True)
                checkpoint = out / 'checkpoint.pt'
                checkpoint.write_bytes(b'fit')
                return checkpoint

            def fake_predict(checkpoint: Path, data: Path, out: Path, recipe: dict[str, Any]) -> list[dict[str, str]]:
                calls.append(('predict', data.name, checkpoint.name))
                predictions = [{**r, 'target': r['G_T_activate'], 'prediction': str(float(r['G_T_activate'])+1),
                                'epistemic_variance': '1', 'aleatoric_variance': '1', 'predictive_variance': '2'}
                               for r in replay_sar_worker.baseline._read_csv(data)]
                replay_sar_worker.baseline._write_csv(out, list(predictions[0]), predictions)
                return predictions

            old_cwd, old_root = Path.cwd(), replay_sar_worker.baseline.ROOT
            try:
                with patch.object(replay_sar_worker, 'fit', side_effect=fake_fit), patch.object(replay_sar_worker, 'predict', side_effect=fake_predict):
                    replay_sar_worker.run(root / 'request.json')
            finally:
                os.chdir(old_cwd)
                replay_sar_worker.baseline.ROOT = old_root
            output = root / 'outputs/round'
            self.assertEqual(calls[0], ('predict', 'newly-revealed.csv', 'previous.pt'))
            self.assertEqual(calls[-2], ('fit', 'validation-train.csv', None))
            model = replay_campaign.read(output / 'model-metrics.json')
            self.assertEqual(model['validation_pairs'], panel)
            self.assertFalse(set(model['training_pairs']) & set(panel))
            pre = replay_campaign.read(output / 'pre-update-metrics.json')
            self.assertEqual(pre['n_pairs'], 2)
            self.assertEqual(pre['checkpoint_sha256'], hashlib.sha256(b'previous main model').hexdigest())
            self.assertNotIn('99999', json.dumps(pre))

    def test_replay_completes_train_download_feedback_select_and_final_pool_without_oracle_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            seed = root / 'seed-00'
            seed.mkdir()
            assets = root / 'assets'
            private = root / 'private'
            assets.mkdir()
            private.mkdir()
            from tricycle_sar.sampling_runtime import stage as stage_sar_runtime

            sar_backend = stage_sar_runtime(assets)
            rows = []
            for i in range(120):
                rows.append({'path_id': f'r{i}', 'source_mapped_reaction_id': f'r{i}',
                             'source_row_index': str(i), 'rxn_smiles': 'CC.C>>CCC',
                             'G_T_activate': str(i+1), 'ene_id': str(i).zfill(12),
                             'diene_id': '000000001000', 'prod_id': '0'})
            fields: list[str] = list(replay_sar_worker.baseline.NATIVE_COLUMNS)
            replay_sar_worker.baseline._write_csv(assets / 'prior-structures.csv', fields, [{**r, 'G_T_activate': '0'} for r in rows])
            replay_sar_worker.baseline._write_csv(private / 'project-labels.csv', fields, rows)
            replay_sar_worker.baseline._write_csv(private / 'evaluation.csv', fields, [{**r, 'G_T_activate': '99999'} for r in rows])
            pool = [{'pair_id': replay_sar_worker.pair(r), 'materialized_sampling_cost_core_hours': '1'} for r in rows]
            replay_sar_worker.baseline._write_csv(private / 'pair-pool.csv', list(pool[0]), pool)
            initial = [p['pair_id'] for p in pool[:40]]
            state: dict[str, Any] = {'seed': 0, 'round': 0, 'phase': 'prepare', 'selected_ids': initial,
                     'new_ids': initial, 'failures': 0, 'revision': 0}
            save(seed / 'state.json', state)
            config = {'experiment_id': 'integration', 'round_count': 3, 'batch_size': 40,
                      'decision_protocol': {'name': getattr(self, 'protocol_name', replay_mae.NAME), 'feedback': True,
                                            'review': getattr(self, 'protocol_review', False),
                                            **({'guidance': self.protocol_guidance} if hasattr(self, 'protocol_guidance') else {}),
                                            **({'evaluation_budgets_core_hours': [40, 80, 120]}
                                               if getattr(self, 'protocol_name', replay_mae.NAME) == replay_core_hours.NAME else {})},
                      'validation_panels': {'0': initial[:8]}, 'source_revision': sar_backend['source_revision'],
                      'sar_backend': sar_backend,
                      'training': {'epochs': 300, 'batch_size_paths': 512, 'device': 'cpu'},
                      'evaluation_sha256': digest(private / 'evaluation.csv'),
                      'assets_hashes': replay_campaign.hashes(assets), 'private_hashes': replay_campaign.hashes(private),
                      'bohr': {'project_id': 'test', 'image': 'test', 'machine_type': 'test'}}
            from tricycle_core.asset_store import externalize_tree
            config['asset_store'] = str(root / 'shared-assets')
            config['asset_references'] = {
                'assets': externalize_tree(assets, Path(config['asset_store'])),
                'private': externalize_tree(private, Path(config['asset_store'])),
            }
            save(root / 'experiment.json', config)
            save(root / 'experiment-hash.json', {'sha256': digest(root / 'experiment.json')})
            jobs: dict[str, Path] = {}

            def fake_fit(data: Path, out: Path, recipe: dict[str, Any], previous: Path | None) -> Path:
                out.mkdir(parents=True)
                checkpoint = out / 'checkpoint.pt'
                checkpoint.write_bytes(b'checkpoint')
                save(out / 'training_summary.json', {'epochs_completed': recipe['epochs']})
                return checkpoint

            def fake_predict(checkpoint: Path, data: Path, out: Path, recipe: dict[str, Any]) -> list[dict[str, str]]:
                result = [{**r, 'target': r['G_T_activate'], 'prediction': str(float(r['G_T_activate'])+1),
                           'epistemic_variance': str(int(r['ene_id']) % 7 + .1),
                           'aleatoric_variance': '1', 'predictive_variance': str(int(r['ene_id']) % 7 + 1.1)}
                          for r in replay_campaign.table(data)]
                replay_campaign.write_table(out, result)
                return result

            class Gateway:
                def create_job_group(self, name: str, project_id: str) -> str:
                    return 'one-group'

                def submit(self, job: Any) -> str:
                    from tricycle_core.asset_store import materialize_input
                    directory = root / f'remote-{len(jobs)}'
                    materialize_input(Path(job.parameters['input_assets_manifest']),
                                      None, directory)
                    old_cwd, old_root = Path.cwd(), replay_sar_worker.baseline.ROOT
                    try:
                        with patch.object(replay_sar_worker, 'fit', side_effect=fake_fit), patch.object(replay_sar_worker, 'predict', side_effect=fake_predict):
                            replay_sar_worker.run(directory / 'request.json')
                    finally:
                        os.chdir(old_cwd)
                        replay_sar_worker.baseline.ROOT = old_root
                    job = str(len(jobs))
                    jobs[job] = directory
                    return job

                def status(self, external_id: str) -> dict[str, Any]:
                    return {'status': 'completed'}

                def download(self, external_id: str, destination: Path) -> tuple[str, ...]:
                    destination.mkdir(parents=True)
                    with zipfile.ZipFile(destination / 'out.zip', 'w') as archive:
                        for path in jobs[external_id].glob('outputs/round/**/*'):
                            if path.is_file():
                                archive.write(path, path.relative_to(jobs[external_id]))
                    return (str(destination / 'out.zip'),)

            model_calls = []

            def fake_agent(task: Path, config: dict[str, Any], *, review_pass: bool = False) -> Path:
                model_calls.append(task)
                brief = replay_campaign.build_decision_brief(task.parent)
                self.assertNotIn('"target"', json.dumps(brief))
                self.assertNotIn('"99999"', json.dumps(brief))
                self.assertNotIn(': 99999', json.dumps(brief))
                core_hours = config['decision_protocol']['name'] == replay_core_hours.NAME
                if core_hours:
                    self.assertEqual(brief['objective'], replay_core_hours.OBJECTIVE)
                    self.assertEqual(brief['core_hour_budget']['unit'], 'core_hours')
                    self.assertIn('CUMULATIVE PROXY CORE-HOUR BUDGETS', replay_mae.prompt(brief))
                    observations = replay_campaign.read(task.parent / 'revealed-cost-observations.json')['observations']
                    revealed = replay_campaign.read(task.parent.parent / 'reveal.json')['selected_ids']
                    self.assertEqual({r['candidate_id'] for r in observations}, set(revealed))
                if getattr(self, 'protocol_guidance', None):
                    self.assertIn('acquisition_guidance', brief)
                    self.assertIn('COVERAGE AND COST', replay_mae.prompt(brief))
                value = {'expression': 'epistemic_variance', 'cost_expression': 'estimated_core_hours' if core_hours else 'product_count * mean_product_atom_count',
                         'rationale': 'Test epistemic uncertainty.', 'author_notes': 'Evidence supports a simple rule.',
                         'decision_action': 'replace', 'hypothesis': 'Uncertain region needs labels.',
                         'expected_effect': 'Reduce regional error.'}
                history = replay_campaign.read(task.parent / 'acquisition-history.json')['previous_rounds']
                response = replay_mae.compile_response(value, replay_campaign.read(task), history[-1]['function'] if history else None)
                response['task_sha256'] = digest(task)
                response_path = task.parent / ('review-response.json' if review_pass else 'response.json')
                save(response_path, response)
                return response_path

            with patch.object(replay_campaign, '_run_agent_once', side_effect=fake_agent):
                for _ in range(25):
                    state = replay_campaign.tick(root, seed, Gateway(), allow_submit=True)
                    self.assertNotEqual(state['phase'], 'blocked', state.get('last_error'))
                    if state['phase'] == 'complete':
                        break
            self.assertEqual(state['phase'], 'complete')
            self.assertEqual(len(jobs), 3)
            self.assertEqual(len(model_calls), 1 + int(getattr(self, 'protocol_review', False)))
            self.assertEqual(len(set(state['selected_ids'])), 120)
            self.assertEqual(len(list(seed.glob('round-*/evaluation-summary.json'))), 3)
            feedback = replay_campaign.read(seed / 'round-0001/decision/feedback.json')
            self.assertTrue(feedback['fixed_panel_comparison']['comparable'])
            self.assertEqual(feedback['previous_batch_outcome']['new_valid_label_pairs'], 40)
            self.assertEqual(replay_campaign.read(seed / 'round-0001/decision/response.json')['decision_source'], 'controller-all-remaining')
            lifecycle = replay_mae_audit.audit_lifecycle({'experiments': [{'root': str(root), 'initial_pair_ids': [initial]}],
                                                        'maximum_sar_jobs': 3})
            self.assertTrue(lifecycle['complete'], lifecycle['failures'])
            self.assertEqual(lifecycle['verified_rounds'], 3)
            evaluation_file = seed / 'round-0002/evaluation-summary.json'
            original_evaluation = replay_campaign.read(evaluation_file)
            save(evaluation_file, {**original_evaluation, 'mae': -1})
            corrupted = replay_mae_audit.audit_lifecycle({'experiments': [{'root': str(root)}], 'maximum_sar_jobs': 3})
            self.assertFalse(corrupted['complete'])
            self.assertTrue(corrupted['failures'])
            save(evaluation_file, original_evaluation)
            selection = seed / 'round-0000/decision/selection.json'
            audited = replay_mae_audit.audit_decision(selection)
            self.assertEqual(audited['candidate_count'], 80)
            self.assertEqual(audited['acquisition_function']['hypothesis'], 'Uncertain region needs labels.')
            if getattr(self, 'protocol_guidance', None):
                decision = seed / 'round-0000/decision'
                assessment = replay_campaign.read(decision / 'formula-assessment.json')
                review = replay_campaign.read(decision / 'proposal-review.json')
                self.assertIn('guidance_comparison', assessment)
                self.assertEqual(assessment['guidance_comparison'], review['guidance_comparison'])
                self.assertEqual(replay_campaign.read(decision / 'review-approved.json')['review_passes'], 1)
                self.assertEqual(audited['guidance_comparison'], assessment['guidance_comparison'])
                reference_path = decision / 'acquisition-guidance.json'
                reference = replay_campaign.read(reference_path)
                altered = {**reference, 'context': {'cost_proxy_median': 999}}
                save(reference_path, altered)
                with self.assertRaisesRegex(ValueError, 'coverage/cost context'):
                    replay_mae_audit.audit_decision(selection)
                save(reference_path, reference)
            score_file = seed / 'exploration/rounds/0001/model-scores.tsv'
            with score_file.open() as handle:
                scored = list(csv.DictReader(handle, delimiter='\t'))
            scored[0]['score'] = '99999'
            with score_file.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(scored[0]), delimiter='\t')
                writer.writeheader()
                writer.writerows(scored)
            with self.assertRaisesRegex(ValueError, 'numeric replay'):
                replay_mae_audit.audit_decision(selection)


class MaeSuiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        experiments = []
        for variant in replay_mae_suite.VARIANTS:
            experiment = self.root / variant
            experiment.mkdir()
            save(experiment / 'experiment.json', {'controller_hashes': {}})
            for i in range(5):
                save(experiment / f'seed-{i:02d}/state.json',
                     {'seed': i, 'phase': 'prepare', 'round': 0, 'selected_ids': list(range(40))})
            experiments.append({'variant': variant, 'root': str(experiment),
                                'config_sha256': digest(experiment / 'experiment.json')})
        save(self.root / 'suite.json', {'suite_id': 'test-suite', 'objective': replay_mae.OBJECTIVE,
                                       'experiments': experiments})
        save(self.root / 'suite-hash.json', {'sha256': digest(self.root / 'suite.json')})

    def test_dry_run_does_not_construct_gateway_or_invoke_agent(self) -> None:
        with patch('tricycle_bohrsubmit.gateway.BohrCliGateway') as gateway, patch.object(replay_mae_suite, 'supervise') as supervise:
            replay_mae_suite.watch(self.root, live=False, allow_submit=False, once=True)
            gateway.assert_not_called()
            supervise.assert_not_called()
        result = replay_campaign.read(self.root / 'status.json')
        self.assertEqual(len(result['states']), 15)
        self.assertEqual(result['completed_evaluations'], 0)
        save(self.root / 'autonomous/experiment.json', {'controller_hashes': {}, 'modified': True})
        with self.assertRaisesRegex(ValueError, 'frozen experiment'):
            replay_mae_suite.watch(self.root, live=False, allow_submit=False, once=True)

    def test_three_strategies_start_concurrently(self) -> None:
        barrier = threading.Barrier(3)
        started: list[str] = []

        def fake_supervise(root: Path, *, live: bool, allow_submit: bool, once: bool) -> None:
            started.append(root.name)
            barrier.wait(timeout=5)
            self.assertTrue(live and allow_submit and once)

        with patch.object(replay_mae_suite, 'supervise', side_effect=fake_supervise):
            replay_mae_suite.watch(self.root, live=True, allow_submit=True, once=True)
        self.assertEqual(set(started), set(replay_mae_suite.VARIANTS))


if __name__ == '__main__':
    unittest.main()
