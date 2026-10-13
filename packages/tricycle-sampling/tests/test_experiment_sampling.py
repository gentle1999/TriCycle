from __future__ import annotations

import copy
import unittest
from typing import Any

from tricycle_core.errors import InvalidRequestError
from tricycle_sampling import (
    LITERATURE_BASELINE_STRATEGIES,
    Candidate,
    experiment_sampling_request,
    select_experiment_candidates,
)


class MatchedSamplingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidates = tuple(Candidate(key, {
            "design_features": [feature] * 6, "product_count": 2,
            "prediction_mean": mean, "predictive_variance": variance + 1,
            "epistemic_variance": variance,
            "target": 999, "future_true_cost": 1000, "success_mask": True,
        }) for key, feature, mean, variance in (
            ("a", 0.0, 20, 9), ("b", 0.001, 30, 8.9), ("c", 1.0, 40, 8)))
        self.arguments: dict[str, Any] = {"reference_features": {"selected": [0.5] * 6},
                          "budget": 2, "seed": 42, "design_size": 3, "offset": 0}

    def test_five_strategies_share_input_contract_and_strip_oracle_fields(self) -> None:
        inputs = []
        for strategy in LITERATURE_BASELINE_STRATEGIES:
            request = experiment_sampling_request(strategy, self.candidates, **self.arguments)
            inputs.append(request.metadata["sampling_experiment"]["shared_inputs_sha256"])
            self.assertTrue(all("target" not in c.payload and "future_true_cost" not in c.payload and "success_mask" not in c.payload for c in request.candidates))
            result = select_experiment_candidates(strategy, self.candidates, **self.arguments)
            self.assertEqual(len(set(result.selected_ids)), 2)
            self.assertTrue(result.metadata["sampling_protocol"]["doi"])
        self.assertEqual(len(set(inputs)), 1)
        changed = tuple(Candidate(c.candidate_id, {**c.payload, "target": -999}) for c in self.candidates)
        for strategy in LITERATURE_BASELINE_STRATEGIES:
            self.assertEqual(select_experiment_candidates(strategy, self.candidates, **self.arguments).selected_ids,
                             select_experiment_candidates(strategy, changed, **self.arguments).selected_ids)

    def test_bayes_uses_rxnresid_epistemic_variance_with_matching_mean_aggregation(self) -> None:
        request = experiment_sampling_request("ucb", self.candidates, **self.arguments)
        self.assertEqual(request.candidates[0].payload["reward_posterior_std"], 1.5)
        self.assertEqual(request.candidates[0].payload["reward_posterior_mean"], 20)
        self.assertEqual(request.metadata["baseline"]["direction"], "minimize")
        changed = tuple(Candidate(c.candidate_id, {**c.payload, "predictive_variance": 100000}) for c in self.candidates)
        self.assertEqual(select_experiment_candidates("ucb", self.candidates, **self.arguments).selected_ids,
                         select_experiment_candidates("ucb", changed, **self.arguments).selected_ids)

    def test_kernel_kb_changes_batch_ranking_and_is_disposable(self) -> None:
        before = copy.deepcopy(self.candidates)
        result = select_experiment_candidates("uncertainty-kriging-believer", self.candidates, **self.arguments)
        self.assertEqual(result.selected_ids, ("a", "c"))
        self.assertEqual(self.candidates, before)
        self.assertEqual(result.to_dict(), select_experiment_candidates("uncertainty-kriging-believer", self.candidates, **self.arguments).to_dict())
        self.assertIn("modeled correlation", result.metadata["sampling_experiment"]["kb_correlation"])
        self.assertEqual(result.metadata["sampling_details"]["parameters"]["adapter_id"], "tricycle.rxnresid-marginals-structural-rbf")

    def test_missing_shared_model_inputs_fail_for_every_strategy(self) -> None:
        missing = tuple(Candidate(c.candidate_id, {key: value for key, value in c.payload.items() if key != "prediction_mean"}) for c in self.candidates)
        for strategy in LITERATURE_BASELINE_STRATEGIES:
            with self.subTest(strategy=strategy), self.assertRaises(InvalidRequestError):
                experiment_sampling_request(strategy, missing, **self.arguments)


if __name__ == "__main__":
    unittest.main()
