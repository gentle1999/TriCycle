from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .errors import InvalidRequestError, PluginRegistrationError
from .models import ExecutionContext, TaskPlan
from .protocols import Component


class SideEffectLevel(StrEnum):
    """The strongest side effect a capability may perform."""

    NONE = "none"
    LOCAL_WRITE = "local-write"
    REMOTE_READ = "remote-read"
    REMOTE_MUTATE = "remote-mutate"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    """Codex-facing metadata for one independently callable capability."""

    capability_id: str
    display_name: str
    version: str
    description: str
    component: str
    input_kind: str
    outputs: tuple[str, ...] = ()
    side_effect: SideEffectLevel = SideEffectLevel.NONE
    requires_auth: bool = False
    supports_dry_run: bool = True
    resumable: bool = False
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "capability_id",
            "display_name",
            "version",
            "description",
            "component",
            "input_kind",
        ):
            if not getattr(self, field_name).strip():
                raise InvalidRequestError(f"{field_name} must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "display_name": self.display_name,
            "version": self.version,
            "description": self.description,
            "component": self.component,
            "input_kind": self.input_kind,
            "outputs": list(self.outputs),
            "side_effect": self.side_effect.value,
            "requires_auth": self.requires_auth,
            "supports_dry_run": self.supports_dry_run,
            "resumable": self.resumable,
            "dependencies": list(self.dependencies),
        }


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    """Bind a public capability ID to a legacy component operation."""

    manifest: CapabilityManifest
    component_id: str
    operation: str
    provider: Component

    def __post_init__(self) -> None:
        if not self.component_id.strip():
            raise InvalidRequestError("component_id must not be empty")
        if not self.operation.strip():
            raise InvalidRequestError("operation must not be empty")
        if self.manifest.component != self.component_id:
            raise InvalidRequestError(
                "capability component does not match the binding: "
                f"{self.manifest.component} != {self.component_id}"
            )
        if self.provider.manifest.plugin_id != self.component_id:
            raise InvalidRequestError(
                "capability component_id does not match the provider manifest: "
                f"{self.component_id} != {self.provider.manifest.plugin_id}"
            )

    def plan(self, request: Mapping[str, Any], context: ExecutionContext) -> TaskPlan:
        plan = self.provider.plan(self.operation, dict(request), context)
        return TaskPlan(
            task_id=plan.task_id,
            plugin_id=plan.plugin_id,
            operation=plan.operation,
            inputs=plan.inputs,
            parameters=plan.parameters,
            metadata={
                **plan.metadata,
                "capability_id": self.manifest.capability_id,
                "side_effect": self.manifest.side_effect.value,
            },
        )

    def validate(self, request: Mapping[str, Any], context: ExecutionContext) -> None:
        """Validate a request through the component's pure planning contract."""

        self.provider.plan(self.operation, dict(request), context)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.manifest.to_dict(),
            "component_id": self.component_id,
            "operation": self.operation,
        }


class CapabilityCatalog:
    """Explicit capability catalog composed by the application layer."""

    def __init__(self) -> None:
        self._bindings: dict[str, CapabilityBinding] = {}

    def register(
        self,
        provider: Component,
        manifest: CapabilityManifest,
        *,
        operation: str,
        replace: bool = False,
    ) -> None:
        capability_id = manifest.capability_id
        if capability_id in self._bindings and not replace:
            raise PluginRegistrationError(f"capability already registered: {capability_id}")
        self._bindings[capability_id] = CapabilityBinding(
            manifest=manifest,
            component_id=provider.manifest.plugin_id,
            operation=operation,
            provider=provider,
        )

    def get(self, capability_id: str) -> CapabilityBinding:
        try:
            return self._bindings[capability_id]
        except KeyError as exc:
            raise PluginRegistrationError(f"unknown capability: {capability_id}") from exc

    def manifests(self) -> tuple[CapabilityManifest, ...]:
        return tuple(binding.manifest for binding in self._bindings.values())

    def bindings(self) -> tuple[CapabilityBinding, ...]:
        return tuple(self._bindings.values())

    def plan(
        self,
        capability_id: str,
        request: Mapping[str, Any],
        context: ExecutionContext,
    ) -> TaskPlan:
        return self.get(capability_id).plan(request, context)

    def validate(
        self,
        capability_id: str,
        request: Mapping[str, Any],
        context: ExecutionContext,
    ) -> None:
        self.get(capability_id).validate(request, context)

    def __contains__(self, capability_id: str) -> bool:
        return capability_id in self._bindings
