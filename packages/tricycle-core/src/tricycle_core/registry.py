from __future__ import annotations

from .errors import PluginRegistrationError
from .models import PluginManifest
from .plugin import PluginBase


class PluginRegistry:
    """Explicit registry used by the application layer to compose plugins."""

    def __init__(self) -> None:
        self._plugins: dict[str, PluginBase] = {}

    def register(self, plugin: PluginBase, *, replace: bool = False) -> None:
        plugin_id = plugin.manifest.plugin_id
        if plugin_id in self._plugins and not replace:
            raise PluginRegistrationError(f"plugin already registered: {plugin_id}")
        self._plugins[plugin_id] = plugin

    def get(self, plugin_id: str) -> PluginBase:
        try:
            return self._plugins[plugin_id]
        except KeyError as exc:
            raise PluginRegistrationError(f"unknown plugin: {plugin_id}") from exc

    def manifests(self) -> tuple[PluginManifest, ...]:
        return tuple(plugin.manifest for plugin in self._plugins.values())

    def plugins(self) -> tuple[PluginBase, ...]:
        return tuple(self._plugins.values())
