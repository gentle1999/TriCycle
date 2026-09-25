from typing import Any

from .diels_alder import DielsAlderEnumeration, enumerate_diels_alder_reactions
from .models import (
    ConformerSamplingConfig,
    DielsAlderEnumerationRequest,
    DielsAlderSubstratePair,
    ReactionConfig,
    ReactionConfigRequest,
    ReactionProfileRequest,
)
from .plugin import ReactionOptPlugin

_LAZY_PROFILE_EXPORTS = frozenset(
    {
        "MappedReaction",
        "MappedReactionProfile",
        "Reaction",
        "ReactionPathContext",
        "ReactionProfile",
        "Species",
        "TransitionStateConformer",
        "TransitionStateConformerSet",
    }
)
_LAZY_RITS_EXPORTS = frozenset(
    {
        "DEFAULT_RITS_CHECKPOINT",
        "DEFAULT_RITS_CHECKPOINT_SOURCE",
        "DEFAULT_RITS_CONFIG",
        "DEFAULT_RITS_IMAGE",
        "DEFAULT_RITS_PIPELINE_SCRIPT",
        "DEFAULT_RITS_SCRIPT",
        "LocalRitsPipelineSampler",
        "RitsInferenceAdapter",
        "RitsInferenceConfig",
        "RitsPipelineConfig",
        "RitsTransitionStateInput",
        "RitsTransitionStateResult",
        "RitsTransitionStateSampler",
        "coerce_sampler_result",
        "molecule_to_xyz",
        "normalize_transition_state_structures",
        "parse_xyz_conformers",
        "write_xyz",
    }
)

_PROFILE_EXPORT_ORDER = (
    "MappedReaction",
    "MappedReactionProfile",
    "Reaction",
    "ReactionPathContext",
    "ReactionProfile",
    "Species",
    "TransitionStateConformer",
    "TransitionStateConformerSet",
)
_RITS_EXPORT_ORDER = (
    "DEFAULT_RITS_CHECKPOINT",
    "DEFAULT_RITS_CHECKPOINT_SOURCE",
    "DEFAULT_RITS_CONFIG",
    "DEFAULT_RITS_IMAGE",
    "DEFAULT_RITS_PIPELINE_SCRIPT",
    "DEFAULT_RITS_SCRIPT",
    "LocalRitsPipelineSampler",
    "RitsInferenceAdapter",
    "RitsInferenceConfig",
    "RitsPipelineConfig",
    "RitsTransitionStateInput",
    "RitsTransitionStateResult",
    "RitsTransitionStateSampler",
    "coerce_sampler_result",
    "molecule_to_xyz",
    "normalize_transition_state_structures",
    "parse_xyz_conformers",
    "write_xyz",
)


def __getattr__(name: str) -> Any:
    if name in _LAZY_PROFILE_EXPORTS:
        from . import reaction_profile

        return getattr(reaction_profile, name)
    if name in _LAZY_RITS_EXPORTS:
        from . import rits

        return getattr(rits, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ConformerSamplingConfig",
    "DielsAlderEnumeration",
    "DielsAlderEnumerationRequest",
    "DielsAlderSubstratePair",
    "ReactionConfig",
    "ReactionConfigRequest",
    "ReactionOptPlugin",
    "ReactionProfileRequest",
    "enumerate_diels_alder_reactions",
    *_PROFILE_EXPORT_ORDER,
    *_RITS_EXPORT_ORDER,
]
