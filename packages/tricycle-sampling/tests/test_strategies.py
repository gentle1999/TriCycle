import hashlib
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tricycle_core import ExecutionContext, InvalidRequestError
from tricycle_sampling import (
    AGENT_ACQUISITION_SCHEMA,
    AleatoricUncertaintySamplingStrategy,
    Candidate,
    EpistemicUncertaintySamplingStrategy,
    PredictiveUncertaintySamplingStrategy,
    SamplingPlugin,
    SamplingRequest,
    decompose_binary_uncertainty,
    get_uncertainty_field,
    get_uncertainty_scores,
)


class UncertaintySamplingTests(unittest.TestCase):
    def test_each_baseline_selects_highest_component_and_preserves_ties(self) -> None:
        candidates = (
            Candidate(
                "rxn-a",
                {"predictive_unc": 0.2, "epistemic_unc": 0.9, "aleatoric_unc": 0.1},
            ),
            Candidate(
                "rxn-b",
                {"predictive_unc": 0.8, "epistemic_unc": 0.2, "aleatoric_unc": 0.7},
            ),
            Candidate(
                "rxn-c",
                {"predictive_unc": 0.8, "epistemic_unc": 0.3, "aleatoric_unc": 0.4},
            ),
        )

        self.assertEqual(
            PredictiveUncertaintySamplingStrategy().select(candidates, 2),
            (candidates[1], candidates[2]),
        )
        self.assertEqual(
            EpistemicUncertaintySamplingStrategy().select(candidates, 1),
            (candidates[0],),
        )
        self.assertEqual(
            AleatoricUncertaintySamplingStrategy().select(candidates, 1),
            (candidates[1],),
        )

    def test_paper_aliases_are_registered(self) -> None:
        plugin = SamplingPlugin()
        candidates = tuple(
            Candidate(
                candidate_id,
                {
                    "predictive_unc": predictive,
                    "epistemic_unc": epistemic,
                    "aleatoric_unc": aleatoric,
                },
            )
            for candidate_id, predictive, epistemic, aleatoric in (
                ("rxn-a", 0.1, 0.3, 0.2),
                ("rxn-b", 0.9, 0.8, 0.7),
            )
        )

        for strategy in (
            "predictive_unc_based",
            "epistemic_unc_based",
            "aleatoric_unc_based",
        ):
            result = plugin.select(
                SamplingRequest(candidates=candidates, budget=1, strategy=strategy)
            )
            self.assertEqual(result.selected_ids, ("rxn-b",))

    def test_rxnresid_evidential_variances_are_sampling_scores(self) -> None:
        candidates = (
            Candidate(
                "rxn-a",
                {
                    "predictive_variance": 0.2,
                    "predictive_std": 0.1,
                    "epistemic_variance": 0.1,
                    "aleatoric_variance": 0.1,
                },
            ),
            Candidate(
                "rxn-b",
                {
                    "predictive_variance": 0.8,
                    "predictive_std": 0.2,
                    "epistemic_variance": 0.6,
                    "aleatoric_variance": 0.2,
                },
            ),
        )
        plugin = SamplingPlugin()

        result = plugin.select(
            SamplingRequest(candidates=candidates, budget=1, strategy="epistemic-uncertainty")
        )

        self.assertEqual(result.selected_ids, ("rxn-b",))
        self.assertEqual(get_uncertainty_scores(candidates[1]).epistemic, 0.6)
        self.assertEqual(get_uncertainty_field(candidates[1], "epistemic"), "epistemic_variance")

        plan = plugin.plan_select(
            SamplingRequest(candidates=candidates, budget=1, strategy="epistemic-uncertainty"),
            ExecutionContext(run_id="rxnresid-uncertainty"),
        )
        self.assertEqual(plan.parameters["score_field"], "epistemic_variance")
        self.assertEqual(
            plan.parameters["score_fields"],
            ["epistemic_variance", "epistemic_variance"],
        )

    def test_posterior_probabilities_are_decomposed_without_external_dependencies(self) -> None:
        scores = decompose_binary_uncertainty((0.01, 0.99))

        self.assertAlmostEqual(scores.predictive, math.log(2.0))
        self.assertAlmostEqual(scores.aleatoric, 0.05600153435484734)
        self.assertAlmostEqual(scores.epistemic, scores.predictive - scores.aleatoric)

        candidate = Candidate("rxn-posterior", {"posterior_probabilities": [0.01, 0.99]})
        self.assertEqual(get_uncertainty_scores(candidate), scores)

    def test_plan_contains_uncertainty_field_and_scores(self) -> None:
        plugin = SamplingPlugin()
        request = SamplingRequest(
            candidates=(Candidate("rxn-a", {"predictive_unc": 0.25, "epistemic_unc": 0.1, "aleatoric_unc": 0.15}),),
            budget=1,
            strategy="predictive-uncertainty",
        )

        plan = plugin.plan_select(request, ExecutionContext(run_id="sampling-test"))

        self.assertEqual(plan.parameters["score_field"], "predictive_unc")
        self.assertEqual(plan.parameters["candidate_scores"], [{"candidate_id": "rxn-a", "score": 0.25}])
        self.assertEqual(plan.metadata["selection"], "highest-score")
        self.assertEqual(plan.metadata["strategy_version"], "1.0.0")

    def test_uncertainty_scores_are_required_when_budget_is_nonzero(self) -> None:
        plugin = SamplingPlugin()
        request = SamplingRequest(
            candidates=(Candidate("rxn-a"),),
            budget=1,
            strategy="epistemic-uncertainty",
        )

        with self.assertRaises(InvalidRequestError):
            plugin.plan_select(request, ExecutionContext(run_id="sampling-test"))

    def test_strategy_only_requires_its_selected_component(self) -> None:
        plugin = SamplingPlugin()
        request = SamplingRequest(
            candidates=(
                Candidate("rxn-low", {"epistemic_unc": 0.1}),
                Candidate("rxn-high", {"epistemic_unc": 0.9}),
            ),
            budget=1,
            strategy="epistemic-uncertainty",
        )

        self.assertEqual(plugin.select(request).selected_ids, ("rxn-high",))

    def test_pair_core_set_uses_previous_pairs_as_references(self) -> None:
        candidates = tuple(
            Candidate(candidate_id, {"pair_embedding": embedding})
            for candidate_id, embedding in (
                ("near-a", 0b0001),
                ("far-b", 0b1111),
                ("near-c", 0b0011),
            )
        )
        plugin = SamplingPlugin()
        result = plugin.select(
            SamplingRequest(
                candidates=candidates,
                budget=1,
                strategy="pair-core-set",
                metadata={"reference_embeddings": {"already-selected": 0b0000}},
            )
        )
        self.assertEqual(result.selected_ids, ("far-b",))

    def test_density_weighted_uncertainty_prefers_dense_uncertain_candidate(self) -> None:
        candidates = (
            Candidate(
                "sparse-high-uncertainty",
                {"predictive_variance": 10.0, "pair_density": 0.1},
            ),
            Candidate(
                "dense-moderate-uncertainty",
                {"predictive_variance": 2.0, "pair_density": 1.0},
            ),
        )
        result = SamplingPlugin().select(
            SamplingRequest(candidates=candidates, budget=1, strategy="density-weighted-uncertainty")
        )
        self.assertEqual(result.selected_ids, ("dense-moderate-uncertainty",))

    def test_uncertainty_diversity_uses_distance_within_a_batch(self) -> None:
        candidates = tuple(
            Candidate(
                candidate_id,
                {"predictive_variance": uncertainty, "pair_embedding": embedding},
            )
            for candidate_id, uncertainty, embedding in (
                ("high-a", 1.0, 0b0001),
                ("high-b", 0.99, 0b0011),
                ("diverse-c", 0.5, 0b1111),
            )
        )
        result = SamplingPlugin().select(
            SamplingRequest(
                candidates=candidates,
                budget=2,
                strategy="uncertainty-diversity",
                seed=0,
                metadata={"diversity_weight": 0.8},
            )
        )
        self.assertEqual(result.selected_ids[0], "high-a")
        self.assertIn("diverse-c", result.selected_ids)


class AgentAuthoredAcquisitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.download_dir = self.root / "downloads" / "worker-001"
        self.download_dir.mkdir(parents=True)
        (self.download_dir / "result.tsv").write_text("completed\n", encoding="utf-8")
        (self.download_dir / "download-manifest.txt").write_text("result.tsv\n", encoding="utf-8")
        report_path = self.root / "download-results.tsv"
        report_path.write_text(
            "worker\ttasks\tjob_id\tjob_name\tstatus\tdownload_dir\n"
            f"worker-001\t1\tbohr-1\tjob-1\tDOWNLOADED\t{self.download_dir}\n",
            encoding="utf-8",
        )
        status_path = self.root / "status.tsv"
        status_path.write_text(
            "worker\ttasks\tjob_id\tjob_name\tsubmit_status\tjob_status\tterminal\tresult\tinput_dir\tchecked_at\n"
            "worker-001\t1\tbohr-1\tjob-1\tSUBMITTED\tCOMPLETED\tyes\tSUCCEEDED\tinputs\t2026-09-24\n",
            encoding="utf-8",
        )
        self.metadata = {
            "agent_acquisition": {
                "schema": AGENT_ACQUISITION_SCHEMA,
                "function_id": "adaptive-round-0001",
                "version": "1.0.0",
                "authored_by": "agent-authored-acquisition",
                "expression": (
                    "epistemic_variance / estimated_cost + context.model_metrics.validation_mae "
                    "* (sampled_novelty + context.sampled_distribution.coverage_gap) / estimated_cost"
                ),
                "cost_expression": "product_count * mean_product_atom_count",
                "rationale": "Prioritize uncertain candidates in poorly covered regions per cost.",
                "context": {
                    "sampled_distribution": {
                        "coverage_gap": 0.25,
                        "sampled_count": 4,
                        "novelty_definition": "nearest-labeled-pair-distance-normalized-v1",
                    },
                    "model_metrics": {"validation_mae": 0.2},
                },
                "evidence": {
                    "sampled_set": "sha256:sampled-set",
                    "candidate_space": "sha256:candidate-space",
                    "model_evaluation": "sha256:model-evaluation",
                    "calculation_status": {
                        "manifest_path": str(status_path.resolve()),
                        "sha256": hashlib.sha256(status_path.read_bytes()).hexdigest(),
                    },
                    "downloaded_results": {
                        "manifest_path": str(report_path.resolve()),
                        "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                    },
                },
            }
        }
        self.candidates = (
            Candidate(
                "rxn-a",
                {
                    "epistemic_variance": 0.9,
                    "product_count": 2,
                    "mean_product_atom_count": 10,
                    "sampled_novelty": 0.9,
                },
            ),
            Candidate(
                "rxn-b",
                {
                    "epistemic_variance": 0.8,
                    "product_count": 4,
                    "mean_product_atom_count": 5,
                    "sampled_novelty": 0.3,
                },
            ),
            Candidate(
                "rxn-c",
                {
                    "epistemic_variance": 0.4,
                    "product_count": 2,
                    "mean_product_atom_count": 10,
                    "sampled_novelty": 0.4,
                },
            ),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_agent_function_scores_with_cost_and_replays_deterministically(self) -> None:
        plugin = SamplingPlugin()
        request = SamplingRequest(
            candidates=self.candidates,
            budget=2,
            strategy="agent-authored-acquisition",
            metadata=self.metadata,
        )

        result = plugin.select(request)
        plan = plugin.plan_select(request, ExecutionContext(run_id="adaptive-test"))

        self.assertEqual(result.selected_ids, ("rxn-a", "rxn-b"))
        self.assertEqual(result.selected_ids, plugin.select(request).selected_ids)
        self.assertEqual(plan.parameters["candidate_scores"][0]["estimated_cost"], 20.0)
        function = plan.parameters["acquisition_function"]
        self.assertEqual(function["cost_summary"]["total"], 60.0)
        self.assertEqual(len(function["function_hash"]), 64)

    def test_agent_function_rejects_python_execution(self) -> None:
        plugin = SamplingPlugin()
        metadata = {
            **self.metadata,
            "agent_acquisition": {
                **self.metadata["agent_acquisition"],
                "expression": "__import__('os').getcwd()",
            },
        }

        with self.assertRaises(InvalidRequestError):
            plugin.plan_select(
                SamplingRequest(
                    candidates=self.candidates,
                    budget=1,
                    strategy="agent-authored-acquisition",
                    metadata=metadata,
                ),
                ExecutionContext(run_id="adaptive-test"),
            )

    def test_agent_function_waits_for_terminal_and_downloaded_workers(self) -> None:
        plugin = SamplingPlugin()
        status_path = Path(
            self.metadata["agent_acquisition"]["evidence"]["calculation_status"]["manifest_path"]
        )
        status_path.write_text(
            "worker\ttasks\tjob_id\tjob_name\tsubmit_status\tjob_status\tterminal\tresult\tinput_dir\tchecked_at\n"
            "worker-001\t1\tbohr-1\tjob-1\tSUBMITTED\tRUNNING\tno\tRUNNING\tinputs\t2026-09-24\n",
            encoding="utf-8",
        )
        metadata = {
            **self.metadata,
            "agent_acquisition": {
                **self.metadata["agent_acquisition"],
                "evidence": {
                    **self.metadata["agent_acquisition"]["evidence"],
                    "calculation_status": {
                        "manifest_path": str(status_path),
                        "sha256": hashlib.sha256(status_path.read_bytes()).hexdigest(),
                    },
                },
            },
        }

        with self.assertRaisesRegex(InvalidRequestError, "every calculation worker to be terminal"):
            plugin.plan_select(
                SamplingRequest(
                    candidates=self.candidates,
                    budget=1,
                    strategy="agent-authored-acquisition",
                    metadata=metadata,
                ),
                ExecutionContext(run_id="adaptive-test"),
            )


if __name__ == "__main__":
    unittest.main()
