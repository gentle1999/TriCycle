from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from tricycle_core.errors import InvalidRequestError

from .models import Candidate


class SamplingStrategy(Protocol):
    name: str

    def select(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
    ) -> tuple[Candidate, ...]: ...


UNCERTAINTY_SCORE_KEYS: dict[str, tuple[str, ...]] = {
    # RxnResid's native evidential regression output is expressed in physical
    # target units. Prefer variance for acquisition, then accept its standard
    # deviation and the historical TriCycle/paper-compatible aliases.
    "predictive": (
        "predictive_variance",
        "predictive_std",
        "predictive_unc",
        "predictive_uncertainty",
    ),
    "epistemic": (
        "epistemic_variance",
        "epistemic_std",
        "epistemic_unc",
        "epistemic_uncertainty",
    ),
    "aleatoric": (
        "aleatoric_variance",
        "aleatoric_std",
        "aleatoric_unc",
        "aleatoric_uncertainty",
    ),
}
POSTERIOR_PROBABILITY_KEYS = ("posterior_probabilities", "posterior_probs")


@dataclass(frozen=True, slots=True)
class UncertaintyScores:
    """Three scalar uncertainty scores for one candidate."""

    predictive: float
    epistemic: float
    aleatoric: float

    def to_dict(self) -> dict[str, float]:
        return {
            "predictive_unc": self.predictive,
            "epistemic_unc": self.epistemic,
            "aleatoric_unc": self.aleatoric,
        }


def _as_probability(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise InvalidRequestError(f"{field_name} must be a number in [0, 1]")
    try:
        probability = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError(f"{field_name} must be a number in [0, 1]") from exc
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise InvalidRequestError(f"{field_name} must be a finite number in [0, 1]")
    return probability


def _as_uncertainty_score(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise InvalidRequestError(f"{field_name} must be a non-negative number")
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError(f"{field_name} must be a non-negative number") from exc
    if not math.isfinite(score) or score < 0.0:
        raise InvalidRequestError(f"{field_name} must be a finite non-negative number")
    return score


def binary_entropy(probability: float) -> float:
    """Return the natural-log entropy of a Bernoulli probability."""

    probability = _as_probability(probability, field_name="probability")
    if probability in (0.0, 1.0):
        return 0.0
    return -probability * math.log(probability) - (1.0 - probability) * math.log1p(
        -probability
    )


def decompose_binary_uncertainty(
    posterior_probabilities: Iterable[float],
) -> UncertaintyScores:
    """Decompose posterior predictive entropy into aleatoric and epistemic parts.

    ``posterior_probabilities`` contains one ``P(y=1 | x, w_i)`` for each
    sampled model parameter set ``w_i``. The decomposition follows the
    entropy identities used by the reference implementation:

    ``predictive = H(mean(p_i))``
    ``aleatoric = mean(H(p_i))``
    ``epistemic = predictive - aleatoric``
    """

    if isinstance(posterior_probabilities, (str, bytes, Mapping)):
        raise InvalidRequestError("posterior probabilities must be a non-empty sequence")
    probabilities = tuple(
        _as_probability(value, field_name="posterior probability")
        for value in posterior_probabilities
    )
    if not probabilities:
        raise InvalidRequestError("posterior probabilities must not be empty")

    predictive = binary_entropy(sum(probabilities) / len(probabilities))
    aleatoric = sum(binary_entropy(probability) for probability in probabilities) / len(
        probabilities
    )
    # The theoretical quantity is non-negative. Clamping only protects
    # against tiny floating-point subtraction errors.
    epistemic = max(0.0, predictive - aleatoric)
    return UncertaintyScores(
        predictive=predictive,
        epistemic=epistemic,
        aleatoric=aleatoric,
    )


_MISSING = object()


def _raw_score(payload: Mapping[str, Any], kind: str) -> Any:
    keys = (*UNCERTAINTY_SCORE_KEYS[kind], kind)
    for key in keys:
        if key in payload:
            return payload[key]

    nested = payload.get("uncertainty", _MISSING)
    if isinstance(nested, Mapping):
        for key in keys:
            if key in nested:
                return nested[key]
    return _MISSING


def _posterior_probabilities(payload: Mapping[str, Any]) -> tuple[float, ...] | None:
    raw = next(
        (payload[key] for key in POSTERIOR_PROBABILITY_KEYS if key in payload),
        _MISSING,
    )
    if raw is _MISSING:
        return None
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Iterable):
        raise InvalidRequestError(
            "posterior_probabilities must be a non-empty sequence of probabilities"
        )
    probabilities = tuple(
        _as_probability(value, field_name="posterior probability") for value in raw
    )
    if not probabilities:
        raise InvalidRequestError("posterior_probabilities must not be empty")
    return probabilities


def get_uncertainty_scores(candidate: Candidate) -> UncertaintyScores:
    """Read or derive the three uncertainty scores for a candidate.

    A model adapter may provide RxnResid's evidential variance/std fields or
    the paper-compatible scalar fields directly. Alternatively, it may
    provide posterior Bernoulli probabilities and let this pure helper
    calculate the entropy decomposition.
    """

    payload = candidate.payload
    raw_values = {kind: _raw_score(payload, kind) for kind in UNCERTAINTY_SCORE_KEYS}
    missing_kinds = [kind for kind, raw in raw_values.items() if raw is _MISSING]
    derived = None
    if missing_kinds:
        posterior = _posterior_probabilities(payload)
        derived = decompose_binary_uncertainty(posterior) if posterior is not None else None

    values: dict[str, float] = {}
    for kind in UNCERTAINTY_SCORE_KEYS:
        raw = raw_values[kind]
        if raw is _MISSING:
            if derived is None:
                expected = ", ".join(UNCERTAINTY_SCORE_KEYS[kind])
                raise InvalidRequestError(
                    f"candidate {candidate.candidate_id!r} is missing {kind} uncertainty; "
                    f"provide payload.{expected} or payload.posterior_probabilities"
                )
            raw = getattr(derived, kind)
        values[kind] = _as_uncertainty_score(
            raw,
            field_name=f"candidate {candidate.candidate_id!r} {kind} uncertainty",
        )

    return UncertaintyScores(
        predictive=values["predictive"],
        epistemic=values["epistemic"],
        aleatoric=values["aleatoric"],
    )


def get_uncertainty_score(candidate: Candidate, kind: str) -> float:
    """Return one uncertainty component, deriving it from posterior samples if needed."""

    if kind not in UNCERTAINTY_SCORE_KEYS:
        raise InvalidRequestError(f"unknown uncertainty kind: {kind}")

    raw = _raw_score(candidate.payload, kind)
    if raw is _MISSING:
        posterior = _posterior_probabilities(candidate.payload)
        if posterior is None:
            expected = ", ".join(UNCERTAINTY_SCORE_KEYS[kind])
            raise InvalidRequestError(
                f"candidate {candidate.candidate_id!r} is missing {kind} uncertainty; "
                f"provide payload.{expected} or payload.posterior_probabilities"
            )
        raw = getattr(decompose_binary_uncertainty(posterior), kind)
    return _as_uncertainty_score(
        raw,
        field_name=f"candidate {candidate.candidate_id!r} {kind} uncertainty",
    )


def get_uncertainty_field(candidate: Candidate, kind: str) -> str:
    """Return the payload field used for one candidate's uncertainty score."""

    if kind not in UNCERTAINTY_SCORE_KEYS:
        raise InvalidRequestError(f"unknown uncertainty kind: {kind}")
    keys = (*UNCERTAINTY_SCORE_KEYS[kind], kind)
    for key in keys:
        if key in candidate.payload:
            return key
    nested = candidate.payload.get("uncertainty", _MISSING)
    if isinstance(nested, Mapping):
        for key in keys:
            if key in nested:
                return f"uncertainty.{key}"
    for key in POSTERIOR_PROBABILITY_KEYS:
        if key in candidate.payload:
            return key
    # Reuse the canonical validation error when no source field exists.
    get_uncertainty_score(candidate, kind)
    return "derived"


class RandomSamplingStrategy:
    name = "random"
    version = "1.0.0"

    def select(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
    ) -> tuple[Candidate, ...]:
        generator = random.Random(seed)
        return tuple(generator.sample(list(candidates), budget))


class UncertaintySamplingStrategy:
    """Select candidates with the largest supplied uncertainty component."""

    name: ClassVar[str]
    uncertainty_kind: ClassVar[str]
    score_key: ClassVar[str]
    version: ClassVar[str] = "1.0.0"

    def score(self, candidate: Candidate) -> float:
        return get_uncertainty_score(candidate, self.uncertainty_kind)

    def validate(
        self,
        candidates: Sequence[Candidate],
        budget: int,
    ) -> None:
        if budget == 0:
            return
        # Ranking requires a score for every candidate, not just the selected
        # ones, so validate the whole pool during planning.
        for candidate in candidates:
            self.score(candidate)

    def select(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
    ) -> tuple[Candidate, ...]:
        del seed
        if budget == 0:
            return ()
        # sorted() is stable, so equal scores retain the input order. This
        # makes batch selection deterministic without a second tie breaker.
        return tuple(sorted(candidates, key=self.score, reverse=True)[:budget])


class PredictiveUncertaintySamplingStrategy(UncertaintySamplingStrategy):
    name = "predictive-uncertainty"
    uncertainty_kind = "predictive"
    score_key = "predictive_unc"


class EpistemicUncertaintySamplingStrategy(UncertaintySamplingStrategy):
    name = "epistemic-uncertainty"
    uncertainty_kind = "epistemic"
    score_key = "epistemic_unc"


class AleatoricUncertaintySamplingStrategy(UncertaintySamplingStrategy):
    name = "aleatoric-uncertainty"
    uncertainty_kind = "aleatoric"
    score_key = "aleatoric_unc"


class FirstNSamplingStrategy:
    name = "first-n"
    version = "1.0.0"

    def select(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
    ) -> tuple[Candidate, ...]:
        del seed
        return tuple(candidates[:budget])


STRATEGY_ALIASES: dict[str, str] = {
    # Names used in the paper's active-learning plots/code.
    "predictive_unc_based": PredictiveUncertaintySamplingStrategy.name,
    "epistemic_unc_based": EpistemicUncertaintySamplingStrategy.name,
    "aleatoric_unc_based": AleatoricUncertaintySamplingStrategy.name,
    "random_selection": RandomSamplingStrategy.name,
    # Readable and column-oriented spellings accepted at the request boundary.
    "predictive_uncertainty": PredictiveUncertaintySamplingStrategy.name,
    "epistemic_uncertainty": EpistemicUncertaintySamplingStrategy.name,
    "aleatoric_uncertainty": AleatoricUncertaintySamplingStrategy.name,
    "predictive_unc": PredictiveUncertaintySamplingStrategy.name,
    "epistemic_unc": EpistemicUncertaintySamplingStrategy.name,
    "aleatoric_unc": AleatoricUncertaintySamplingStrategy.name,
}
