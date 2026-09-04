from tricycle_bohr import BohrComputePlugin
from tricycle_bohrsubmit import BohrSubmitPlugin
from tricycle_core import CapabilityCatalog, PluginRegistry
from tricycle_reaction_db import ReactionDatabasePlugin
from tricycle_reactionopt import ReactionOptPlugin
from tricycle_rits import RitsPlugin
from tricycle_sampling import SamplingPlugin
from tricycle_sar import SarPlugin


def create_registry() -> PluginRegistry:
    registry = PluginRegistry()
    for plugin in (
        BohrComputePlugin(),
        BohrSubmitPlugin(),
        ReactionDatabasePlugin(),
        SarPlugin(),
        RitsPlugin(),
        SamplingPlugin(),
        ReactionOptPlugin(),
    ):
        registry.register(plugin)
    return registry


def create_capability_catalog(registry: PluginRegistry | None = None) -> CapabilityCatalog:
    """Create the Codex-facing capability catalog over the composed components."""

    from .capabilities import create_capability_catalog as _create_capability_catalog

    return _create_capability_catalog(registry or create_registry())
