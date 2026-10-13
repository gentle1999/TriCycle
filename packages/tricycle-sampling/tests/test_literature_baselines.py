from __future__ import annotations

import copy
import json
import math
import unittest
from collections.abc import Mapping, Sequence
from typing import Any

from tricycle_core import ExecutionContext
from tricycle_core.errors import InvalidRequestError
from tricycle_sampling import (
    LITERATURE_BASELINE_STRATEGIES,
    LITERATURE_BASELINES,
    Candidate,
    KrigingBelieverSession,
    LatinHypercubeSamplingStrategy,
    SamplingPlugin,
    SamplingRequest,
    SurrogatePrediction,
    get_baseline_protocol,
)


class LiteratureBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = SamplingPlugin()

    def test_profile_binds_five_distinct_papers_and_excludes_random(self) -> None:
        self.assertEqual(len(LITERATURE_BASELINE_STRATEGIES), 5)
        self.assertEqual(len({p.doi for p in LITERATURE_BASELINES}), 5)
        self.assertNotIn("random", LITERATURE_BASELINE_STRATEGIES)
        self.assertNotIn("epistemic-uncertainty", LITERATURE_BASELINE_STRATEGIES)
        self.assertTrue(set(LITERATURE_BASELINE_STRATEGIES) <= set(self.plugin.strategies))
        # Random sampling is an independent strategy outside the literature profile.
        self.assertIn("random", self.plugin.strategies)
        record = get_baseline_protocol("ucb").to_dict()
        record["doi"] = "changed"
        self.assertEqual(get_baseline_protocol("ucb").doi, "10.1038/s41586-024-07021-y")

    def test_predictive_provenance_cannot_be_overridden_and_zero_budget_needs_no_scores(
        self,
    ) -> None:
        request = SamplingRequest(
            (
                Candidate("b", {"posterior_probabilities": [0.2, 0.8]}),
                Candidate("a", {"posterior_probabilities": [0.5, 0.5]}),
            ),
            1,
            "predictive-uncertainty",
            metadata={"sampling_protocol": {"doi": "fake"}},
        )
        result = self.plugin.select(request)
        plan = self.plugin.plan_select(request, ExecutionContext(run_id="literature-test"))
        self.assertEqual(result.selected_ids, ("a",))
        for record in (result.metadata["sampling_protocol"], plan.metadata["sampling_protocol"]):
            self.assertEqual(record["doi"], "10.1038/s41467-025-59812-0")
            self.assertEqual(record["strategy_version"], "1.1.0")
            self.assertEqual(len(record["sha256"]), 64)
        json.dumps(result.to_dict(), allow_nan=False)
        empty = SamplingRequest((Candidate("unscored"),), 0, "predictive-uncertainty")
        self.assertEqual(self.plugin.select(empty).selected_ids, ())
        self.plugin.plan_select(empty, ExecutionContext(run_id="empty"))

    def test_lhs_is_stratified_and_prefix_batch_mapping_is_replayable(self) -> None:
        strategy = LatinHypercubeSamplingStrategy()
        points = strategy.design_points(7, 2, 21)
        for dimension in range(2):
            self.assertEqual(sorted(math.floor(p[dimension] * 7) for p in points), list(range(7)))
        candidates = tuple(
            Candidate(str(i), {"design_features": [i / 9, (9 - i) / 9]}) for i in range(10)
        )
        metadata = {"baseline": {"bounds": [[0, 1], [0, 1]], "design_size": 7}}
        full = self.plugin.select(SamplingRequest(candidates, 7, strategy.name, 21, metadata))
        first = self.plugin.select(
            SamplingRequest(tuple(reversed(candidates)), 3, strategy.name, 21, metadata)
        )
        second_metadata = {"baseline": {**metadata["baseline"], "offset": 3}}
        second = self.plugin.select(
            SamplingRequest(first.remaining, 4, strategy.name, 21, second_metadata)
        )
        self.assertEqual(full.selected_ids, first.selected_ids + second.selected_ids)
        self.assertEqual(len(set(full.selected_ids)), 7)
        self.assertEqual(
            full.selected,
            self.plugin.select(
                SamplingRequest(candidates, 7, strategy.name, 21, metadata)
            ).selected,
        )
        self.assertEqual(full.metadata["sampling_protocol"]["doi"], "10.1126/science.adj1817")

    def test_lhs_rejects_missing_seed_bounds_and_categorical_bitsets(self) -> None:
        for payload, seed, options in (
            ({"design_features": [0.5]}, None, {"bounds": [[0, 1]]}),
            ({"design_features": [0.5]}, 1, {}),
            ({"design_features": 42}, 1, {"bounds": [[0, 1]]}),
            ({"design_features": [2]}, 1, {"bounds": [[0, 1]]}),
            ({"design_features": [0.5]}, 1, {"bounds": [[0, 1]], "design_size": 0}),
        ):
            with self.subTest(payload=payload, seed=seed, options=options):
                request = SamplingRequest(
                    (Candidate("a", payload),), 1, "latin-hypercube", seed, {"baseline": options}
                )
                with self.assertRaises(InvalidRequestError):
                    self.plugin.plan_select(request, ExecutionContext(run_id="invalid"))

    def test_bayes_ucb_uses_reward_posterior_and_direction_not_predictive_variance(self) -> None:
        candidates = (
            Candidate(
                "a",
                {"reward_posterior_mean": 1, "reward_posterior_std": 1, "predictive_variance": 999},
            ),
            Candidate(
                "b",
                {"reward_posterior_mean": 3, "reward_posterior_std": 0, "predictive_variance": 0},
            ),
        )
        request = SamplingRequest(candidates, 1, "ucb")
        self.assertEqual(self.plugin.select(request).selected_ids, ("b",))
        explore = SamplingRequest(candidates, 1, "ucb", metadata={"baseline": {"quantile": 0.999}})
        self.assertEqual(self.plugin.select(explore).selected_ids, ("a",))
        minimize = SamplingRequest(
            candidates, 1, "ucb", metadata={"baseline": {"direction": "minimize"}}
        )
        self.assertEqual(self.plugin.select(minimize).selected_ids, ("a",))
        self.assertEqual(
            self.plugin.select(explore).metadata["sampling_protocol"]["doi"],
            "10.1038/s41586-024-07021-y",
        )
        with self.assertRaises(InvalidRequestError):
            self.plugin.select(
                SamplingRequest(
                    (Candidate("x", {"predictive_variance": 1, "prediction": 1}),), 1, "ucb"
                )
            )

    def test_ucb_plan_binding_changes_with_posterior_and_rejects_invalid_options(self) -> None:
        def plan(mean: float) -> Any:
            return self.plugin.plan_select(
                SamplingRequest(
                    (Candidate("a", {"reward_posterior_mean": mean, "reward_posterior_std": 1}),),
                    1,
                    "ucb",
                ),
                ExecutionContext(run_id="plan"),
            )

        self.assertNotEqual(
            plan(1).metadata["sampling_protocol"]["decision_sha256"],
            plan(2).metadata["sampling_protocol"]["decision_sha256"],
        )
        for options in ({"quantile": 1}, {"quantile": True}, {"direction": "other"}, {"beta": 1}):
            with self.subTest(options=options), self.assertRaises(InvalidRequestError):
                self.plugin.select(
                    SamplingRequest(
                        (Candidate("a", {"reward_posterior_mean": 1, "reward_posterior_std": 1}),),
                        1,
                        "ucb",
                        metadata={"baseline": options},
                    )
                )

    def test_a_score_is_product_with_nearest_labeled_distance_not_additive_or_density(self) -> None:
        candidates = (
            Candidate(
                "near", {"design_features": [1], "predictive_variance": 10, "pair_density": 100}
            ),
            Candidate(
                "far", {"design_features": [5], "predictive_variance": 3, "pair_density": 0.01}
            ),
        )
        metadata = {"baseline": {"reference_features": {"labeled": [0]}}}
        result = self.plugin.select(
            SamplingRequest(candidates, 2, "distance-uncertainty-product", metadata=metadata)
        )
        self.assertEqual(result.selected_ids, ("far", "near"))
        self.assertEqual(
            [row["score"] for row in result.metadata["sampling_details"]["candidate_scores"]],
            [10, 15],
        )
        self.assertEqual(result.metadata["sampling_protocol"]["doi"], "10.1038/s41467-024-49011-8")
        for refs in ({}, {"near": [0]}, {"labeled": [0, 1]}):
            with self.subTest(refs=refs), self.assertRaises(InvalidRequestError):
                self.plugin.select(
                    SamplingRequest(
                        candidates,
                        1,
                        "distance-uncertainty-product",
                        metadata={"baseline": {"reference_features": refs}},
                    )
                )

    def test_zero_budget_new_methods_preserve_pool_without_requiring_predictions(self) -> None:
        for strategy in LITERATURE_BASELINE_STRATEGIES[1:]:
            with self.subTest(strategy=strategy):
                request = SamplingRequest((Candidate("a"),), 0, strategy)
                result = self.plugin.select(request)
                self.plugin.plan_select(request, ExecutionContext(run_id="zero"))
                self.assertEqual(result.selected_ids, ())
                self.assertEqual(result.remaining, request.candidates)


class KrigingBelieverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidates = tuple(Candidate(key) for key in ("a", "b", "c"))
        self.metadata = {
            "baseline": {
                "posterior": {
                    "candidate_ids": ["a", "b", "c"],
                    "means": [20, 30, 40],
                    "covariance": [[9, 8, 0], [8, 8.9, 0], [0, 0, 8]],
                }
            }
        }

    def test_kb_changes_second_selection_with_pseudo_observation_and_leaves_input_untouched(
        self,
    ) -> None:
        metadata = copy.deepcopy(self.metadata)
        request = SamplingRequest(self.candidates, 2, "uncertainty-kriging-believer", 42, metadata)
        plugin = SamplingPlugin()
        result = plugin.select(request)
        self.assertEqual(result.selected_ids, ("a", "c"))  # Static top-k would choose a,b.
        steps = result.metadata["sampling_details"]["steps"]
        self.assertEqual(steps[0]["pseudo_label"], 20)
        before = {row["candidate_id"]: row["std"] for row in steps[0]["candidate_scores"]}
        after = {row["candidate_id"]: row["std"] for row in steps[1]["candidate_scores"]}
        self.assertLess(after["b"], before["b"])
        self.assertEqual(after["c"], before["c"])
        self.assertEqual(metadata, self.metadata)
        self.assertEqual(result.to_dict(), plugin.select(request).to_dict())
        self.assertTrue(all(not c.payload for c in result.selected))
        self.assertEqual(result.metadata["sampling_protocol"]["doi"], "10.1021/jacs.5c02218")
        json.dumps(result.to_dict(), allow_nan=False)

    def test_kb_rejects_unbound_non_psd_asymmetric_and_invalid_matrices(self) -> None:
        for covariance in (
            [[1, 2, 0], [2, 1, 0], [0, 0, 1]],
            [[1, 1, 0], [0, 1, 0], [0, 0, 1]],
            [[1, 0], [0, 1]],
            [[1, 0, 0], [0, float("nan"), 0], [0, 0, 1]],
        ):
            metadata = copy.deepcopy(self.metadata)
            metadata["baseline"]["posterior"]["covariance"] = covariance
            with self.subTest(covariance=covariance), self.assertRaises(InvalidRequestError):
                SamplingPlugin().plan_select(
                    SamplingRequest(
                        self.candidates, 2, "uncertainty-kriging-believer", metadata=metadata
                    ),
                    ExecutionContext(run_id="invalid"),
                )
        metadata = copy.deepcopy(self.metadata)
        metadata["baseline"]["posterior"]["candidate_ids"] = ["a", "a", "c"]
        with self.assertRaises(InvalidRequestError):
            SamplingPlugin().select(
                SamplingRequest(
                    self.candidates, 2, "uncertainty-kriging-believer", metadata=metadata
                )
            )

    def test_singular_covariance_and_zero_uncertainty_are_supported(self) -> None:
        metadata = copy.deepcopy(self.metadata)
        metadata["baseline"]["posterior"]["covariance"] = [[1, 1, 0], [1, 1, 0], [0, 0, 0]]
        result = SamplingPlugin().select(
            SamplingRequest(
                tuple(reversed(self.candidates)),
                3,
                "uncertainty-kriging-believer",
                metadata=metadata,
            )
        )
        self.assertEqual(result.selected_ids, ("a", "b", "c"))

    def test_adapter_planning_does_not_create_or_refit_session(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.labels: list[float] = []

            def predict(self, candidates: Sequence[Candidate]) -> Mapping[str, SurrogatePrediction]:
                return {c.candidate_id: SurrogatePrediction(7, 1) for c in candidates}

            def believe(self, candidate: Candidate, predicted_label: float) -> None:
                self.labels.append(predicted_label)

        class Factory:
            adapter_id = "test.local-refit"
            version = "1.0.0"

            def __init__(self) -> None:
                self.sessions: list[Session] = []

            def validate(
                self, candidates: Sequence[Candidate], metadata: Mapping[str, Any]
            ) -> None:
                pass

            def create(
                self, candidates: Sequence[Candidate], metadata: Mapping[str, Any], seed: int | None
            ) -> KrigingBelieverSession:
                session = Session()
                self.sessions.append(session)
                return session

        factory = Factory()
        plugin = SamplingPlugin(kriging_believer_factory=factory)
        request = SamplingRequest(self.candidates, 2, "uncertainty-kriging-believer")
        plan = plugin.plan_select(request, ExecutionContext(run_id="adapter-plan"))
        self.assertEqual(factory.sessions, [])
        self.assertEqual(plan.parameters["baseline"]["adapter_id"], "test.local-refit")
        self.assertEqual(plugin.select(request).selected_ids, ("a", "b"))
        self.assertEqual(factory.sessions[0].labels, [7])
        plugin.select(request)
        self.assertEqual(len(factory.sessions), 2)


if __name__ == "__main__":
    unittest.main()
