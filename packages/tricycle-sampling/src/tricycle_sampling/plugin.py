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
from .strategies import FirstNSamplingStrategy, RandomSamplingStrategy, SamplingStrategy


class SamplingPlugin(PluginBase):
    _manifest = PluginManifest(
        plugin_id="tricycle.sampling",
        display_name="Sampling strategy",
        version="0.1.0",
        kind=PluginKind.SAMPLING,
        description="Select the next reaction candidates using pluggable deterministic strategies.",
        capabilities=("candidate.validate", "candidate.select", "strategy.register"),
    )

    def __init__(self, strategies: tuple[SamplingStrategy, ...] = ()) -> None:
        builtins = (RandomSamplingStrategy(), FirstNSamplingStrategy())
        self._strategies: dict[str, SamplingStrategy] = {
            strategy.name: strategy for strategy in builtins
        }
        for strategy in strategies:
            self.register_strategy(strategy)

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    def register_strategy(self, strategy: SamplingStrategy, *, replace: bool = False) -> None:
        if strategy.name in self._strategies and not replace:
            raise InvalidRequestError(f"sampling strategy already registered: {strategy.name}")
        self._strategies[strategy.name] = strategy

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
        return TaskPlan(
            task_id=f"{context.run_id}/sampling/{request.strategy}",
            plugin_id=self.manifest.plugin_id,
            operation="select",
            parameters={
                "strategy": request.strategy,
                "budget": request.budget,
                "seed": request.seed,
                "candidates": [candidate.to_dict() for candidate in request.candidates],
            },
            metadata={"candidate_count": len(request.candidates), "dry_run": context.dry_run},
        )

    def select(self, request: SamplingRequest) -> SamplingResult:
        try:
            strategy = self._strategies[request.strategy]
        except KeyError as exc:
            raise InvalidRequestError(f"unknown sampling strategy: {request.strategy}") from exc
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
