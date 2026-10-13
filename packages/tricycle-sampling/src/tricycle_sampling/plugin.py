from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from tricycle_core import (
    ExecutionContext,
    PluginBase,
    PluginKind,
    PluginManifest,
    TaskPlan,
)
from tricycle_core.errors import InvalidRequestError

from .acquisition import AgentAuthoredAcquisitionSamplingStrategy
from .literature import (
    BayesUCBSamplingStrategy,
    DistanceUncertaintyProductSamplingStrategy,
    KrigingBelieverFactory,
    LatinHypercubeSamplingStrategy,
    LiteratureSamplingStrategy,
    UncertaintyKrigingBelieverSamplingStrategy,
)
from .models import Candidate, SamplingRequest, SamplingResult
from .protocols import protocol_hash
from .strategies import (
    AleatoricUncertaintySamplingStrategy,
    DensityWeightedUncertaintySamplingStrategy,
    EpistemicUncertaintySamplingStrategy,
    FirstNSamplingStrategy,
    PairCoreSetSamplingStrategy,
    PredictiveUncertaintySamplingStrategy,
    RandomSamplingStrategy,
    SamplingStrategy,
    UncertaintyDiversitySamplingStrategy,
    UncertaintySamplingStrategy,
    get_uncertainty_field,
)


class SamplingPlugin(PluginBase):
    _manifest = PluginManifest(
        plugin_id="tricycle.sampling",
        display_name="Sampling strategy",
        version="0.2.0",
        kind=PluginKind.SAMPLING,
        description=(
            "Select the next reaction candidates with registered baselines or a validated, "
            "agent-authored adaptive acquisition expression."
        ),
        capabilities=("candidate.validate", "candidate.select", "strategy.register"),
    )

    def __init__(
        self, strategies: tuple[SamplingStrategy, ...] = (), *,
        kriging_believer_factory: KrigingBelieverFactory | None = None,
    ) -> None:
        self._strategies: dict[str, SamplingStrategy] = {}
        defaults: tuple[SamplingStrategy, ...] = (
            RandomSamplingStrategy(),
            FirstNSamplingStrategy(),
            PredictiveUncertaintySamplingStrategy(),
            EpistemicUncertaintySamplingStrategy(),
            AleatoricUncertaintySamplingStrategy(),
            LatinHypercubeSamplingStrategy(),
            BayesUCBSamplingStrategy(),
            DistanceUncertaintyProductSamplingStrategy(),
            UncertaintyKrigingBelieverSamplingStrategy(kriging_believer_factory),
            PairCoreSetSamplingStrategy(),
            DensityWeightedUncertaintySamplingStrategy(),
            UncertaintyDiversitySamplingStrategy(),
            AgentAuthoredAcquisitionSamplingStrategy(),
        )
        for strategy in defaults:
            self._strategies[strategy.name] = strategy
        for strategy in strategies:
            self.register_strategy(strategy)

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    def register_strategy(self, strategy: SamplingStrategy, *, replace: bool = False) -> None:
        if strategy.name in self._strategies and not replace:
            raise InvalidRequestError(f"sampling strategy already registered: {strategy.name}")
        self._strategies[strategy.name] = strategy

    @property
    def strategies(self) -> tuple[str, ...]:
        """Return canonical strategy names in registration order."""

        return tuple(self._strategies)

    def _resolve_strategy(self, name: str) -> SamplingStrategy:
        try:
            return self._strategies[name]
        except KeyError:
            raise InvalidRequestError(f"unknown sampling strategy: {name}") from None

    def plan(
        self,
        operation: str,
        request: dict[str, Any],
        context: ExecutionContext,
    ) -> TaskPlan:
        if operation != "select":
            raise InvalidRequestError(f"unsupported sampling operation: {operation}")
        return self.plan_select(SamplingRequest.from_mapping(request), context)

    def plan_select(self, request: SamplingRequest, context: ExecutionContext) -> TaskPlan:
        strategy = self._resolve_strategy(request.strategy)
        contextual_planner = getattr(strategy, "plan_with_context", None)
        validator = getattr(strategy, "validate", None)
        if callable(validator):
            validator(request.candidates, request.budget)
        contextual_validator = getattr(strategy, "validate_with_context", None)
        if callable(contextual_validator) and not callable(contextual_planner):
            contextual_validator(request.candidates, request.budget, request.metadata)

        parameters: dict[str, Any] = {
            "strategy": request.strategy,
            "budget": request.budget,
            "seed": request.seed,
            "candidates": [candidate.to_dict() for candidate in request.candidates],
        }
        metadata: dict[str, Any] = {
            "candidate_count": len(request.candidates),
            "dry_run": context.dry_run,
            "strategy_version": getattr(strategy, "version", self.manifest.version),
        }
        if isinstance(strategy, LiteratureSamplingStrategy):
            parameters["baseline"] = strategy.prepare(
                request.candidates, request.budget, request.seed, request.metadata,
            )
            parameters["baseline_inputs"] = dict(request.metadata.get("baseline", {}))
        elif callable(contextual_planner):
            plan_with_context = cast(
                Callable[[Sequence[Candidate], int, Mapping[str, Any]], dict[str, Any]],
                contextual_planner,
            )
            contextual_parameters = plan_with_context(
                request.candidates,
                request.budget,
                request.metadata,
            )
            parameters.update(contextual_parameters)
            acquisition = contextual_parameters.get("acquisition_function", {})
            metadata.update(
                {
                    "strategy_version": getattr(strategy, "version", self.manifest.version),
                    "selection": "highest-score",
                    "acquisition_function_hash": (
                        acquisition.get("function_hash", "")
                        if isinstance(acquisition, dict)
                        else ""
                    ),
                }
            )
        elif isinstance(strategy, UncertaintySamplingStrategy):
            score_fields = [
                get_uncertainty_field(candidate, strategy.uncertainty_kind)
                for candidate in request.candidates if request.budget
            ]
            distinct_score_fields = tuple(dict.fromkeys(score_fields))
            parameters["score_field"] = (
                distinct_score_fields[0] if len(distinct_score_fields) == 1 else "mixed"
            )
            parameters["score_fields"] = score_fields
            parameters["candidate_scores"] = [
                {
                    "candidate_id": candidate.candidate_id,
                    "score": strategy.score(candidate),
                }
                for candidate in request.candidates if request.budget
            ]
            metadata.update(
                {
                    "strategy_version": getattr(strategy, "version", self.manifest.version),
                    "selection": "highest-score",
                }
            )
        provenance = self._baseline_provenance(strategy, request, parameters)
        if provenance:
            metadata["sampling_protocol"] = provenance
        return TaskPlan(
            task_id=f"{context.run_id}/sampling/{request.strategy}",
            plugin_id=self.manifest.plugin_id,
            operation="select",
            parameters=parameters,
            metadata=metadata,
        )

    def select(self, request: SamplingRequest) -> SamplingResult:
        strategy = self._resolve_strategy(request.strategy)
        details: dict[str, Any] = {}
        contextual_select = getattr(strategy, "select_with_context", None)
        if isinstance(strategy, LiteratureSamplingStrategy):
            selected, details = strategy.select_detailed(
                request.candidates, request.budget, request.seed, request.metadata,
            )
        elif callable(contextual_select):
            select_with_context = cast(
                Callable[[Sequence[Candidate], int, int | None, Mapping[str, Any]], tuple[Candidate, ...]],
                contextual_select,
            )
            selected = select_with_context(
                request.candidates,
                request.budget,
                request.seed,
                request.metadata,
            )
        else:
            selected = strategy.select(request.candidates, request.budget, request.seed)
        if isinstance(strategy, PredictiveUncertaintySamplingStrategy):
            details = {
                "candidate_scores": [
                    {"candidate_id": candidate.candidate_id,
                     "score_field": get_uncertainty_field(candidate, "predictive"),
                     "score": strategy.score(candidate)}
                    for candidate in request.candidates if request.budget
                ],
            }
        selected_ids = {candidate.candidate_id for candidate in selected}
        remaining = tuple(
            candidate
            for candidate in request.candidates
            if candidate.candidate_id not in selected_ids
        )
        result_metadata = dict(request.metadata)
        provenance = self._baseline_provenance(strategy, request, details)
        if provenance:
            result_metadata["sampling_protocol"] = provenance
            result_metadata["sampling_details"] = details
        return SamplingResult(
            strategy=request.strategy,
            selected=selected,
            remaining=remaining,
            seed=request.seed,
            metadata=result_metadata,
        )

    @staticmethod
    def _baseline_provenance(
        strategy: SamplingStrategy, request: SamplingRequest, details: Mapping[str, Any],
    ) -> dict[str, Any]:
        record = getattr(strategy, "baseline_protocol", None)
        if not isinstance(record, dict):
            return {}
        return {
            **record,
            "strategy_version": getattr(strategy, "version", ""),
            "requested_strategy": request.strategy,
            "seed": request.seed,
            "budget": request.budget,
            "decision_sha256": protocol_hash(details),
        }
