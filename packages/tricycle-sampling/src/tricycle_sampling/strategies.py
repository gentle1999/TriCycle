from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from tricycle_core.errors import InvalidRequestError

from .models import Candidate
from .protocols import get_baseline_protocol


class SamplingStrategy(Protocol):
    @property
    def name(self) -> str: ...

    def select(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
    ) -> tuple[Candidate, ...]: ...


Embedding = int | tuple[float, ...]
_EMBEDDING_KEYS = ("pair_embedding", "embedding")


def _embedding_value(value: Any, *, field_name: str) -> Embedding:
    """Validate the compact binary or dense embedding used by batch strategies."""

    if isinstance(value, bool):
        raise InvalidRequestError(f"{field_name} must be a non-negative integer or numeric sequence")
    if isinstance(value, int):
        if value < 0:
            raise InvalidRequestError(f"{field_name} must be a non-negative integer")
        return value
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise InvalidRequestError(
            f"{field_name} must be a non-negative integer or numeric sequence"
        )
    values: list[float] = []
    for index, item in enumerate(value):
        if isinstance(item, bool):
            raise InvalidRequestError(f"{field_name}[{index}] must be a finite number")
        try:
            parsed = float(item)
        except (TypeError, ValueError) as exc:
            raise InvalidRequestError(f"{field_name}[{index}] must be a finite number") from exc
        if not math.isfinite(parsed):
            raise InvalidRequestError(f"{field_name}[{index}] must be a finite number")
        values.append(parsed)
    if not values:
        raise InvalidRequestError(f"{field_name} must not be empty")
    return tuple(values)


def _candidate_embedding(candidate: Candidate) -> Embedding:
    for key in _EMBEDDING_KEYS:
        if key in candidate.payload:
            return _embedding_value(candidate.payload[key], field_name=f"candidate {candidate.candidate_id!r} {key}")
    raise InvalidRequestError(
        f"candidate {candidate.candidate_id!r} is missing pair_embedding or embedding"
    )


def _embedding_distance(left: Embedding, right: Embedding) -> float:
    """Return Hamming distance for bitsets and Euclidean distance otherwise."""

    if isinstance(left, int) and isinstance(right, int):
        return float((left ^ right).bit_count())
    if isinstance(left, int) or isinstance(right, int):
        raise InvalidRequestError("all candidate embeddings must use the same representation")
    if len(left) != len(right):
        raise InvalidRequestError("all candidate embeddings must have the same dimension")
    return math.sqrt(sum((left[index] - right[index]) ** 2 for index in range(len(left))))


def _reference_embeddings(metadata: Mapping[str, Any] | None) -> tuple[Embedding, ...]:
    if not metadata:
        return ()
    raw = metadata.get("reference_embeddings", {})
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise InvalidRequestError("selection metadata.reference_embeddings must be a mapping")
    return tuple(
        _embedding_value(value, field_name=f"reference_embeddings[{candidate_id!r}]")
        for candidate_id, value in raw.items()
    )


def _min_distance(embedding: Embedding, references: Sequence[Embedding]) -> float:
    if not references:
        return 0.0
    return min(_embedding_distance(embedding, reference) for reference in references)


def _diversity_weight(metadata: Mapping[str, Any] | None) -> float:
    value = 0.5 if not metadata else metadata.get("diversity_weight", 0.5)
    try:
        weight = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError("diversity_weight must be a number in [0, 1]") from exc
    if not math.isfinite(weight) or not 0.0 <= weight <= 1.0:
        raise InvalidRequestError("diversity_weight must be a number in [0, 1]")
    return weight


UNCERTAINTY_SCORE_KEYS: dict[str, tuple[str, ...]] = {
    # RxnResid's native evidential regression output is expressed in physical
    # target units. Variance, standard deviation and generic scalar uncertainty
    # are distinct score representations, in that precedence order.
    "predictive": (
        "predictive_variance",
        "predictive_std",
        "predictive_uncertainty",
    ),
    "epistemic": (
        "epistemic_variance",
        "epistemic_std",
        "epistemic_uncertainty",
    ),
    "aleatoric": (
        "aleatoric_variance",
        "aleatoric_std",
        "aleatoric_uncertainty",
    ),
}


@dataclass(frozen=True, slots=True)
class UncertaintyScores:
    """Three scalar uncertainty scores for one candidate."""

    predictive: float
    epistemic: float
    aleatoric: float

    def to_dict(self) -> dict[str, float]:
        return {
            "predictive_uncertainty": self.predictive,
            "epistemic_uncertainty": self.epistemic,
            "aleatoric_uncertainty": self.aleatoric,
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


def _validate_uncertainty_fields(payload: Mapping[str, Any]) -> None:
    obsolete = {"uncertainty", "posterior_probs"}
    obsolete.update(UNCERTAINTY_SCORE_KEYS)
    obsolete.update(f"{kind}_unc" for kind in UNCERTAINTY_SCORE_KEYS)
    invalid = sorted(set(payload) & obsolete)
    if invalid:
        raise InvalidRequestError(
            "unsupported uncertainty fields: " + ", ".join(invalid)
            + "; use *_variance, *_std, *_uncertainty or posterior_probabilities"
        )


def _raw_score(payload: Mapping[str, Any], kind: str) -> Any:
    _validate_uncertainty_fields(payload)
    for key in UNCERTAINTY_SCORE_KEYS[kind]:
        if key in payload:
            return payload[key]
    return _MISSING


def _posterior_probabilities(payload: Mapping[str, Any]) -> tuple[float, ...] | None:
    raw = payload.get("posterior_probabilities", _MISSING)
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
    generic scalar *_uncertainty fields directly. Alternatively, it may
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
    _validate_uncertainty_fields(candidate.payload)
    for key in UNCERTAINTY_SCORE_KEYS[kind]:
        if key in candidate.payload:
            return key
    if "posterior_probabilities" in candidate.payload:
        return "posterior_probabilities"
    # Reuse the canonical validation error when no source field exists.
    get_uncertainty_score(candidate, kind)
    return "derived"


class RandomSamplingStrategy:
    name = "random"
    version = "1.1.0"

    def select(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
    ) -> tuple[Candidate, ...]:
        generator = random.Random(seed)
        # Canonicalize the input so a fixed seed maps a candidate set to the
        # same sampled subset even if the pool arrived in a different order.
        ordered = sorted(candidates, key=lambda candidate: candidate.candidate_id)
        return tuple(generator.sample(ordered, budget))


class UncertaintySamplingStrategy:
    """Select candidates with the largest supplied uncertainty component."""

    name: ClassVar[str]
    uncertainty_kind: ClassVar[str]
    score_key: ClassVar[str]
    version: ClassVar[str] = "1.1.0"

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
        # Candidate IDs provide an explicit tie-break instead of relying on
        # the incoming pool order.
        ranked = sorted(
            candidates,
            key=lambda candidate: (-self.score(candidate), candidate.candidate_id),
        )
        return tuple(ranked[:budget])


class PredictiveUncertaintySamplingStrategy(UncertaintySamplingStrategy):
    name = "predictive-uncertainty"
    uncertainty_kind = "predictive"
    score_key = "predictive_uncertainty"

    @property
    def baseline_protocol(self) -> dict[str, Any]:
        return get_baseline_protocol(self.name).to_dict()


class EpistemicUncertaintySamplingStrategy(UncertaintySamplingStrategy):
    name = "epistemic-uncertainty"
    uncertainty_kind = "epistemic"
    score_key = "epistemic_uncertainty"


class AleatoricUncertaintySamplingStrategy(UncertaintySamplingStrategy):
    name = "aleatoric-uncertainty"
    uncertainty_kind = "aleatoric"
    score_key = "aleatoric_uncertainty"


class PairCoreSetSamplingStrategy:
    """Greedy k-center sampling over substrate-pair embeddings.

    ``selection metadata.reference_embeddings`` should contain the embeddings
    of substrate pairs selected in earlier rounds.  The strategy then chooses
    candidates farthest from the already selected set and from one another.
    A compact integer bitset is supported so molecular fingerprints do not
    need a numerical dependency in the sampling package.
    """

    name = "pair-core-set"
    version = "1.1.0"

    def validate(self, candidates: Sequence[Candidate], budget: int) -> None:
        if budget == 0:
            return
        for candidate in candidates:
            _candidate_embedding(candidate)

    def select_with_context(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[Candidate, ...]:
        del seed
        if budget == 0:
            return ()
        if budget > len(candidates):
            raise InvalidRequestError("sampling budget cannot exceed candidate count")
        embeddings = tuple(_candidate_embedding(candidate) for candidate in candidates)
        references = list(_reference_embeddings(metadata))
        remaining = set(range(len(candidates)))
        selected: list[int] = []

        for _ in range(budget):
            if not selected and not references:
                index = min(
                    remaining,
                    key=lambda candidate_index: candidates[candidate_index].candidate_id,
                )
            else:
                reference_pool = [*references, *(embeddings[index] for index in selected)]
                index = min(
                    remaining,
                    key=lambda candidate_index: (
                        -_min_distance(embeddings[candidate_index], reference_pool),
                        candidates[candidate_index].candidate_id,
                    ),
                )
            remaining.remove(index)
            selected.append(index)
        return tuple(candidates[index] for index in selected)

    def select(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
    ) -> tuple[Candidate, ...]:
        return self.select_with_context(candidates, budget, seed, {})


class DensityWeightedUncertaintySamplingStrategy(UncertaintySamplingStrategy):
    """Prefer uncertain candidates in dense regions of substrate space."""

    name = "density-weighted-uncertainty"
    uncertainty_kind = "predictive"
    score_key = "density_weighted_predictive_unc"
    version = "1.1.0"

    def density(self, candidate: Candidate) -> float:
        if "density" in candidate.payload:
            raise InvalidRequestError("unsupported density field: density; use pair_density")
        raw = candidate.payload.get("pair_density", _MISSING)
        if raw is _MISSING:
            raise InvalidRequestError(
                f"candidate {candidate.candidate_id!r} is missing pair_density"
            )
        density = _as_uncertainty_score(
            raw,
            field_name=f"candidate {candidate.candidate_id!r} pair density",
        )
        return density

    def score(self, candidate: Candidate) -> float:
        return super().score(candidate) * self.density(candidate)

    def validate(self, candidates: Sequence[Candidate], budget: int) -> None:
        super().validate(candidates, budget)
        if budget:
            for candidate in candidates:
                self.density(candidate)


class UncertaintyDiversitySamplingStrategy(UncertaintySamplingStrategy):
    """Greedily balance predictive uncertainty and substrate-space coverage."""

    name = "uncertainty-diversity"
    uncertainty_kind = "predictive"
    score_key = "predictive_unc_with_diversity"
    version = "1.1.0"

    def validate(self, candidates: Sequence[Candidate], budget: int) -> None:
        super().validate(candidates, budget)
        if budget:
            for candidate in candidates:
                _candidate_embedding(candidate)

    def select_with_context(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[Candidate, ...]:
        del seed
        if budget == 0:
            return ()
        if budget > len(candidates):
            raise InvalidRequestError("sampling budget cannot exceed candidate count")
        embeddings = tuple(_candidate_embedding(candidate) for candidate in candidates)
        uncertainties = tuple(self._raw_uncertainty(candidate) for candidate in candidates)
        minimum = min(uncertainties)
        spread = max(uncertainties) - minimum
        normalized_uncertainties = tuple(
            (value - minimum) / spread if spread else 0.0 for value in uncertainties
        )
        references = list(_reference_embeddings(metadata))
        weight = _diversity_weight(metadata)
        remaining = set(range(len(candidates)))
        selected: list[int] = []

        for _ in range(budget):
            reference_pool = [*references, *(embeddings[index] for index in selected)]
            distances = {
                index: _min_distance(embeddings[index], reference_pool)
                for index in remaining
            }
            maximum_distance = max(distances.values(), default=0.0)
            selected_index = min(
                remaining,
                key=lambda index: (
                    -(
                        (1.0 - weight) * normalized_uncertainties[index]
                        + weight
                        * (distances[index] / maximum_distance if maximum_distance else 0.0)
                    ),
                    candidates[index].candidate_id,
                ),
            )
            remaining.remove(selected_index)
            selected.append(selected_index)
        return tuple(candidates[index] for index in selected)

    def _raw_uncertainty(self, candidate: Candidate) -> float:
        return get_uncertainty_score(candidate, self.uncertainty_kind)

    def select(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
    ) -> tuple[Candidate, ...]:
        return self.select_with_context(candidates, budget, seed, {})


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
