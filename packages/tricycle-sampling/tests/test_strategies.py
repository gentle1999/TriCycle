import math
import unittest

from tricycle_core import ExecutionContext, InvalidRequestError
from tricycle_sampling import (
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


if __name__ == "__main__":
    unittest.main()
