"""Deterministic, local adaptations of the four additional literature baselines."""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any, Protocol

from tricycle_core.errors import InvalidRequestError

from .models import Candidate
from .protocols import get_baseline_protocol, protocol_hash


def _number(value: Any, name: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise InvalidRequestError(f"{name} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidRequestError(f"{name} must be a finite number") from exc
    if not math.isfinite(parsed) or (nonnegative and parsed < 0):
        raise InvalidRequestError(
            f"{name} must be finite" + (" and nonnegative" if nonnegative else "")
        )
    return parsed


def _vector(value: Any, name: str) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise InvalidRequestError(f"{name} must be a nonempty numeric vector")
    return tuple(_number(item, name) for item in value)


def _options(metadata: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
    value = metadata.get("baseline", {})
    if not isinstance(value, Mapping):
        raise InvalidRequestError("metadata.baseline must be a mapping")
    unknown = set(value) - allowed
    if unknown:
        raise InvalidRequestError(f"unknown baseline options: {sorted(unknown)}")
    return dict(value)


def _features(candidates: Sequence[Candidate], field: str) -> dict[str, tuple[float, ...]]:
    result = {c.candidate_id: _vector(c.payload.get(field), field) for c in candidates}
    if len({len(v) for v in result.values()}) != 1:
        raise InvalidRequestError("feature dimensions must match")
    return result


class LiteratureSamplingStrategy:
    name: str
    version = "1.0.0"

    @property
    def baseline_protocol(self) -> dict[str, Any]:
        return get_baseline_protocol(self.name).to_dict()

    def prepare(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    def select_detailed(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None,
        metadata: Mapping[str, Any],
    ) -> tuple[tuple[Candidate, ...], dict[str, Any]]:
        raise NotImplementedError

    def select_with_context(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[Candidate, ...]:
        return self.select_detailed(candidates, budget, seed, metadata or {})[0]

    def select(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
    ) -> tuple[Candidate, ...]:
        return self.select_with_context(candidates, budget, seed)


class LatinHypercubeSamplingStrategy(LiteratureSamplingStrategy):
    name = "latin-hypercube"

    def prepare(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        options = _options(metadata, {"feature_field", "bounds", "design_size", "offset"})
        if budget == 0:
            return {"no_op": True}
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise InvalidRequestError("latin-hypercube requires an integer seed")
        field = str(options.get("feature_field", "design_features"))
        features = _features(candidates, field)
        dimension = len(next(iter(features.values())))
        raw_bounds = options.get("bounds")
        if not isinstance(raw_bounds, Sequence) or len(raw_bounds) != dimension:
            raise InvalidRequestError("LHS requires fixed bounds for every feature dimension")
        bounds = [_vector(row, "bounds") for row in raw_bounds]
        if any(len(row) != 2 or row[0] >= row[1] for row in bounds):
            raise InvalidRequestError("each bound must contain lower < upper")
        for lo, hi in bounds:
            _number(hi - lo, "bound width", nonnegative=True)
        for vector in features.values():
            if any(not lo <= v <= hi for v, (lo, hi) in zip(vector, bounds, strict=True)):
                raise InvalidRequestError("design_features must lie within fixed bounds")
        size = options.get("design_size", budget)
        offset = options.get("offset", 0)
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (size, offset)):
            raise InvalidRequestError("design_size and offset must be integers")
        if offset < 0 or size < offset + budget:
            raise InvalidRequestError("design_size must cover offset + budget")
        return {
            "feature_field": field,
            "bounds": bounds,
            "design_size": size,
            "offset": offset,
            "mapping": "nearest-unused-normalized-euclidean",
            "features_sha256": protocol_hash(features),
        }

    def design_points(self, size: int, dimension: int, seed: int) -> tuple[tuple[float, ...], ...]:
        """Generate a normalized Latin hypercube; stable prefixes require a fixed size."""
        generator = random.Random(seed)
        columns = []
        for _ in range(dimension):
            strata = list(range(size))
            generator.shuffle(strata)
            columns.append([(stratum + generator.random()) / size for stratum in strata])
        return tuple(tuple(column[i] for column in columns) for i in range(size))

    def select_detailed(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None,
        metadata: Mapping[str, Any],
    ) -> tuple[tuple[Candidate, ...], dict[str, Any]]:
        config = self.prepare(candidates, budget, seed, metadata)
        if budget == 0:
            return (), {"parameters": config, "steps": []}
        assert seed is not None
        bounds = config["bounds"]
        features = _features(candidates, config["feature_field"])
        normalized = {
            key: tuple((v - lo) / (hi - lo) for v, (lo, hi) in zip(values, bounds, strict=True))
            for key, values in features.items()
        }
        points = self.design_points(config["design_size"], len(bounds), seed)
        remaining = {c.candidate_id: c for c in candidates}
        selected = []
        steps = []
        for point in points[config["offset"] : config["offset"] + budget]:
            distances = {key: math.dist(point, normalized[key]) for key in remaining}
            key = min(remaining, key=lambda key: (distances[key], key))
            selected.append(remaining.pop(key))
            steps.append(
                {"candidate_id": key, "design_point": list(point), "distance": distances[key]}
            )
        return tuple(selected), {"parameters": config, "steps": steps}


class BayesUCBSamplingStrategy(LiteratureSamplingStrategy):
    name = "ucb"

    def prepare(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        options = _options(metadata, {"quantile", "direction"})
        quantile = _number(options.get("quantile", 0.95), "quantile")
        if not 0.5 < quantile < 1:
            raise InvalidRequestError("Bayes-UCB quantile must be in (0.5, 1)")
        direction = options.get("direction", "maximize")
        if direction not in ("maximize", "minimize"):
            raise InvalidRequestError("direction must be maximize or minimize")
        posteriors = {}
        if budget:
            for c in candidates:
                posteriors[c.candidate_id] = {
                    "mean": _number(
                        c.payload.get("reward_posterior_mean"), "reward_posterior_mean"
                    ),
                    "std": _number(
                        c.payload.get("reward_posterior_std"),
                        "reward_posterior_std",
                        nonnegative=True,
                    ),
                }
        return {
            "variant": "gaussian-bayes-ucb",
            "quantile": quantile,
            "direction": direction,
            "posterior": "upstream expected-reward posterior; one arm per candidate",
            "posterior_sha256": protocol_hash(posteriors),
        }

    def select_detailed(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None,
        metadata: Mapping[str, Any],
    ) -> tuple[tuple[Candidate, ...], dict[str, Any]]:
        config = self.prepare(candidates, budget, seed, metadata)
        z = NormalDist().inv_cdf(config["quantile"])
        entries: list[dict[str, Any]] = []
        for c in candidates if budget else ():
            mean = _number(c.payload["reward_posterior_mean"], "reward_posterior_mean")
            std = _number(
                c.payload["reward_posterior_std"], "reward_posterior_std", nonnegative=True
            )
            score = (mean if config["direction"] == "maximize" else -mean) + z * std
            entries.append({"candidate_id": c.candidate_id, "score": _number(score, "UCB score")})
        scores = {e["candidate_id"]: e["score"] for e in entries}
        ranked = (
            sorted(candidates, key=lambda c: (-scores[c.candidate_id], c.candidate_id))
            if budget
            else []
        )
        return tuple(ranked[:budget]), {"parameters": config, "candidate_scores": entries}


class DistanceUncertaintyProductSamplingStrategy(LiteratureSamplingStrategy):
    name = "distance-uncertainty-product"

    def prepare(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        options = _options(metadata, {"feature_field", "variance_field", "reference_features"})
        field = str(options.get("feature_field", "design_features"))
        variance = str(options.get("variance_field", "predictive_variance"))
        config: dict[str, Any] = {
            "feature_field": field,
            "variance_field": variance,
            "distance": "nearest-reference-euclidean",
            "batch": "fixed-reference-top-k",
        }
        if not budget:
            return config
        features = _features(candidates, field)
        raw = options.get("reference_features")
        if not isinstance(raw, Mapping) or not raw:
            raise InvalidRequestError("A Score requires nonempty labeled reference_features")
        references = {str(key): _vector(value, "reference_features") for key, value in raw.items()}
        if set(references) & set(features):
            raise InvalidRequestError("labeled references must be disjoint from the candidate pool")
        dimension = len(next(iter(features.values())))
        if any(len(v) != dimension for v in references.values()):
            raise InvalidRequestError("reference and candidate feature dimensions must match")
        for c in candidates:
            _number(c.payload.get(variance), variance, nonnegative=True)
        config["reference_features"] = references
        config["features_sha256"] = protocol_hash(features)
        return config

    def select_detailed(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None,
        metadata: Mapping[str, Any],
    ) -> tuple[tuple[Candidate, ...], dict[str, Any]]:
        config = self.prepare(candidates, budget, seed, metadata)
        entries: list[dict[str, Any]] = []
        for c in candidates if budget else ():
            vector = _vector(c.payload[config["feature_field"]], config["feature_field"])
            distance = min(math.dist(vector, ref) for ref in config["reference_features"].values())
            variance = _number(c.payload[config["variance_field"]], "variance", nonnegative=True)
            entries.append(
                {
                    "candidate_id": c.candidate_id,
                    "distance": distance,
                    "variance": variance,
                    "score": _number(distance * variance, "A Score", nonnegative=True),
                }
            )
        scores = {e["candidate_id"]: e["score"] for e in entries}
        ranked = (
            sorted(candidates, key=lambda c: (-scores[c.candidate_id], c.candidate_id))
            if budget
            else []
        )
        return tuple(ranked[:budget]), {"parameters": config, "candidate_scores": entries}


@dataclass(frozen=True, slots=True)
class SurrogatePrediction:
    mean: float
    std: float


class KrigingBelieverSession(Protocol):
    """A fresh, disposable local surrogate; never mutates the formal training set."""

    def predict(self, candidates: Sequence[Candidate]) -> Mapping[str, SurrogatePrediction]: ...

    def believe(self, candidate: Candidate, predicted_label: float) -> None:
        """Condition/refit using this temporary prediction, before the next predict."""
        ...


class KrigingBelieverFactory(Protocol):
    adapter_id: str
    version: str

    def validate(self, candidates: Sequence[Candidate], metadata: Mapping[str, Any]) -> None:
        """Validate local inputs only; planning must not train or access external systems."""
        ...

    def create(
        self, candidates: Sequence[Candidate], metadata: Mapping[str, Any], seed: int | None
    ) -> KrigingBelieverSession: ...


class GaussianKrigingBelieverSession:
    def __init__(
        self,
        ids: Sequence[str],
        means: Sequence[float],
        covariance: Sequence[Sequence[float]],
        noise: float,
    ) -> None:
        self.index = {key: i for i, key in enumerate(ids)}
        self.means = list(means)
        self.covariance = [list(row) for row in covariance]
        self.noise = noise

    def predict(self, candidates: Sequence[Candidate]) -> Mapping[str, SurrogatePrediction]:
        return {
            c.candidate_id: SurrogatePrediction(
                self.means[self.index[c.candidate_id]],
                math.sqrt(
                    max(
                        0.0, self.covariance[self.index[c.candidate_id]][self.index[c.candidate_id]]
                    )
                ),
            )
            for c in candidates
        }

    def believe(self, candidate: Candidate, predicted_label: float) -> None:
        i = self.index[candidate.candidate_id]
        denominator = self.covariance[i][i] + self.noise
        if denominator <= 0:
            return
        column = [row[i] for row in self.covariance]
        residual = predicted_label - self.means[i]
        self.means = [
            mean + value * residual / denominator
            for mean, value in zip(self.means, column, strict=True)
        ]
        self.covariance = [
            [value - column[j] * column[k] / denominator for k, value in enumerate(row)]
            for j, row in enumerate(self.covariance)
        ]


class GaussianKrigingBelieverFactory:
    """Exact Gaussian pseudo-observation update of an upstream joint posterior."""

    adapter_id = "tricycle.gaussian-joint-posterior"
    version = "1.0.0"

    def _inputs(
        self, candidates: Sequence[Candidate], metadata: Mapping[str, Any]
    ) -> tuple[list[str], list[float], list[tuple[float, ...]], float]:
        options = _options(metadata, {"posterior", "observation_noise_variance"})
        posterior = options.get("posterior")
        if not isinstance(posterior, Mapping):
            raise InvalidRequestError(
                "KB requires baseline.posterior with candidate_ids, means, covariance"
            )
        if set(posterior) != {"candidate_ids", "means", "covariance"}:
            raise InvalidRequestError(
                "posterior must contain only candidate_ids, means, covariance"
            )
        ids = posterior.get("candidate_ids")
        if not isinstance(ids, Sequence) or isinstance(ids, (str, bytes)):
            raise InvalidRequestError("posterior.candidate_ids must be a sequence")
        ids = list(ids)
        if any(not isinstance(key, str) for key in ids) or len(ids) != len(set(ids)):
            raise InvalidRequestError("posterior candidate IDs must be unique strings")
        if set(ids) != {c.candidate_id for c in candidates}:
            raise InvalidRequestError(
                "posterior candidate IDs must exactly match the candidate pool"
            )
        means = list(_vector(posterior.get("means"), "posterior.means"))
        raw = posterior.get("covariance")
        if not isinstance(raw, Sequence) or len(raw) != len(ids) or len(means) != len(ids):
            raise InvalidRequestError("posterior dimensions must match candidate IDs")
        covariance = [_vector(row, "covariance row") for row in raw]
        if any(len(row) != len(ids) for row in covariance):
            raise InvalidRequestError("covariance must be square")
        # LDL factorization checks positive semidefiniteness, including singular matrices.
        scale = max(1.0, max(abs(value) for row in covariance for value in row))
        tolerance = 1e-10 * scale
        lower = [[0.0] * len(ids) for _ in ids]
        diagonal: list[float] = []
        for i in range(len(ids)):
            for j in range(i):
                if abs(covariance[i][j] - covariance[j][i]) > tolerance:
                    raise InvalidRequestError("covariance must be symmetric")
                residual = covariance[i][j] - sum(
                    lower[i][k] * lower[j][k] * diagonal[k] for k in range(j)
                )
                if diagonal[j] == 0:
                    if abs(residual) > tolerance:
                        raise InvalidRequestError("covariance must be positive semidefinite")
                else:
                    lower[i][j] = residual / diagonal[j]
            pivot = covariance[i][i] - sum(lower[i][k] ** 2 * diagonal[k] for k in range(i))
            if covariance[i][i] < 0 or pivot < -tolerance:
                raise InvalidRequestError("covariance must be positive semidefinite")
            diagonal.append(max(0.0, pivot))
            lower[i][i] = 1.0
        noise = _number(
            options.get("observation_noise_variance", 0.0),
            "observation_noise_variance",
            nonnegative=True,
        )
        return ids, means, covariance, noise

    def validate(self, candidates: Sequence[Candidate], metadata: Mapping[str, Any]) -> None:
        self._inputs(candidates, metadata)

    def create(
        self, candidates: Sequence[Candidate], metadata: Mapping[str, Any], seed: int | None
    ) -> GaussianKrigingBelieverSession:
        return GaussianKrigingBelieverSession(*self._inputs(candidates, metadata))


class UncertaintyKrigingBelieverSamplingStrategy(LiteratureSamplingStrategy):
    name = "uncertainty-kriging-believer"

    def __init__(self, factory: KrigingBelieverFactory | None = None) -> None:
        self.factory = factory if factory is not None else GaussianKrigingBelieverFactory()

    def prepare(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        if budget:
            self.factory.validate(candidates, metadata)
        return {
            "adapter_id": self.factory.adapter_id,
            "adapter_version": self.factory.version,
            "adapter_inputs_sha256": protocol_hash(metadata.get("baseline", {})),
            "score": "surrogate standard deviation",
            "pseudo_labels": "batch-local-only",
        }

    def select_detailed(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None,
        metadata: Mapping[str, Any],
    ) -> tuple[tuple[Candidate, ...], dict[str, Any]]:
        config = self.prepare(candidates, budget, seed, metadata)
        if budget == 0:
            return (), {"parameters": config, "steps": []}
        session = self.factory.create(candidates, metadata, seed)
        remaining = sorted(candidates, key=lambda c: c.candidate_id)
        selected = []
        steps = []
        for index in range(budget):
            predictions = session.predict(remaining)
            if set(predictions) != {c.candidate_id for c in remaining}:
                raise InvalidRequestError("KB surrogate must predict every remaining candidate")
            scores: list[dict[str, Any]] = []
            for c in remaining:
                prediction = predictions[c.candidate_id]
                scores.append(
                    {
                        "candidate_id": c.candidate_id,
                        "mean": _number(prediction.mean, "surrogate mean"),
                        "std": _number(prediction.std, "surrogate std", nonnegative=True),
                    }
                )
            winner = min(scores, key=lambda row: (-row["std"], row["candidate_id"]))
            candidate = next(c for c in remaining if c.candidate_id == winner["candidate_id"])
            selected.append(candidate)
            remaining.remove(candidate)
            steps.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "pseudo_label": winner["mean"],
                    "candidate_scores": scores,
                    "conditioned_before_next": index + 1 < budget,
                }
            )
            if index + 1 < budget:
                session.believe(candidate, winner["mean"])
        return tuple(selected), {"parameters": config, "steps": steps}
