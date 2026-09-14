from __future__ import annotations

from typing import Any

from tricycle_core import (
    ExecutionContext,
    PluginBase,
    PluginKind,
    PluginManifest,
    TaskPlan,
)
from tricycle_core.errors import InvalidRequestError

from .models import SamplingRequest, SamplingResult
from .strategies import (
    STRATEGY_ALIASES,
    AleatoricUncertaintySamplingStrategy,
    EpistemicUncertaintySamplingStrategy,
    FirstNSamplingStrategy,
    PredictiveUncertaintySamplingStrategy,
    RandomSamplingStrategy,
    SamplingStrategy,
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
            "Select the next reaction candidates with random, first-n, or uncertainty-aware "
            "strategies."
        ),
        capabilities=("candidate.validate", "candidate.select", "strategy.register"),
    )

    def __init__(self, strategies: tuple[SamplingStrategy, ...] = ()) -> None:
        self._strategies: dict[str, SamplingStrategy] = {}
        for strategy in (
            RandomSamplingStrategy(),
            FirstNSamplingStrategy(),
            PredictiveUncertaintySamplingStrategy(),
            EpistemicUncertaintySamplingStrategy(),
            AleatoricUncertaintySamplingStrategy(),
        ):
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
            canonical_name = STRATEGY_ALIASES.get(name)
            if canonical_name is not None:
                return self._strategies[canonical_name]
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
        if isinstance(strategy, UncertaintySamplingStrategy):
            strategy.validate(request.candidates, request.budget)

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
        if isinstance(strategy, UncertaintySamplingStrategy):
            score_fields = [
                get_uncertainty_field(candidate, strategy.uncertainty_kind)
                for candidate in request.candidates
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
                for candidate in request.candidates
            ]
            metadata.update(
                {
                    "strategy_version": strategy.version,
                    "selection": "highest-score",
                }
            )
        return TaskPlan(
            task_id=f"{context.run_id}/sampling/{request.strategy}",
            plugin_id=self.manifest.plugin_id,
            operation="select",
            parameters=parameters,
            metadata=metadata,
        )

    def select(self, request: SamplingRequest) -> SamplingResult:
        strategy = self._resolve_strategy(request.strategy)
        selected = strategy.select(request.candidates, request.budget, request.seed)
        selected_ids = {candidate.candidate_id for candidate in selected}
        remaining = tuple(
            candidate
            for candidate in request.candidates
            if candidate.candidate_id not in selected_ids
        )
        return SamplingResult(
            strategy=request.strategy,
            selected=selected,
            remaining=remaining,
            seed=request.seed,
            metadata=dict(request.metadata),
        )
