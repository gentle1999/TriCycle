"""One input adapter and selection entry point for matched literature experiments.

No labels, evaluation metrics, costs or external inference enter this adapter.
RxnResid supplies marginal epistemic variances. The KB joint covariance uses an
explicit structural RBF correlation approximation, not a claimed SAR joint posterior.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

from tricycle_core.errors import InvalidRequestError

from .literature import SurrogatePrediction, _number, _vector
from .models import Candidate, SamplingRequest, SamplingResult
from .protocols import LITERATURE_BASELINE_STRATEGIES, protocol_hash

EXPERIMENT_SAMPLING_SCHEMA = "tricycle.matched-sampling/v1"
STRUCTURE_FEATURE_NAMES = (
    "reactant_heavy_atoms",
    "reactant_heteroatoms",
    "reactant_rings",
    "reactant_aromatic_atoms",
    "reactant_rotatable_bonds",
    "reactant_hbond_acceptors",
)


def bounded_structure_features(counts: Sequence[float]) -> tuple[float, ...]:
    """Frozen, label-free count/(1+count) transform, shared by every strategy."""
    if len(counts) != len(STRUCTURE_FEATURE_NAMES):
        raise InvalidRequestError("six structural descriptors required")
    parsed = tuple(_number(v, "structure count", nonnegative=True) for v in counts)
    return tuple(value / (1.0 + value) for value in parsed)


@lru_cache(maxsize=8192)
def reactant_structure_features(smiles: str) -> tuple[float, ...]:
    """Explicit optional RDKit adapter, shared by both experiment controllers."""
    from rdkit import Chem
    from rdkit.Chem import rdMolDescriptors

    molecule = Chem.MolFromSmiles(smiles.split(">>")[0])
    if molecule is None:
        raise InvalidRequestError("invalid reactant structure for matched sampling")
    return bounded_structure_features(
        (
            molecule.GetNumHeavyAtoms(),
            sum(atom.GetAtomicNum() not in (1, 6) for atom in molecule.GetAtoms()),
            rdMolDescriptors.CalcNumRings(molecule),
            sum(atom.GetIsAromatic() for atom in molecule.GetAtoms()),
            rdMolDescriptors.CalcNumRotatableBonds(molecule),
            rdMolDescriptors.CalcNumHBA(molecule),
        )
    )


class StructuralKrigingBelieverSession:
    """Lazy Gaussian conditioning; O(pool × batch²), without a dense pool matrix."""

    def __init__(self, candidates: Sequence[Candidate], length_scale: float = 1.0) -> None:
        self.features = {
            c.candidate_id: _vector(c.payload["design_features"], "design_features")
            for c in candidates
        }
        self.means = {c.candidate_id: float(c.payload["reward_posterior_mean"]) for c in candidates}
        self.variances = {
            c.candidate_id: float(c.payload["reward_posterior_std"]) ** 2 for c in candidates
        }
        self.basis: list[dict[str, float]] = []
        self.length_scale = length_scale

    def _covariance(self, left: str, right: str) -> float:
        distance = math.dist(self.features[left], self.features[right])
        return math.sqrt(self.variances[left]) * math.sqrt(self.variances[right]) * math.exp(
            -0.5 * (distance / self.length_scale) ** 2
        ) - sum(column[left] * column[right] for column in self.basis)

    def predict(self, candidates: Sequence[Candidate]) -> Mapping[str, SurrogatePrediction]:
        return {
            c.candidate_id: SurrogatePrediction(
                self.means[c.candidate_id],
                math.sqrt(max(0.0, self._covariance(c.candidate_id, c.candidate_id))),
            )
            for c in candidates
        }

    def believe(self, candidate: Candidate, predicted_label: float) -> None:
        key = candidate.candidate_id
        variance = max(0.0, self._covariance(key, key))
        if variance <= 1e-14 * max(1.0, self.variances[key]):
            return
        residual = predicted_label - self.means[key]
        covariance = {other: self._covariance(other, key) for other in self.features}
        for other, value in covariance.items():
            self.means[other] += value / variance * residual
        self.basis.append(
            {other: value / math.sqrt(variance) for other, value in covariance.items()}
        )


class StructuralKrigingBelieverFactory:
    adapter_id = "tricycle.rxnresid-marginals-structural-rbf"
    version = "1.0.0"

    def validate(self, candidates: Sequence[Candidate], metadata: Mapping[str, Any]) -> None:
        if metadata.get("baseline") != {"length_scale": 1.0}:
            raise InvalidRequestError(
                "matched KB requires the frozen structural RBF length scale 1.0"
            )
        for c in candidates:
            features = _vector(c.payload.get("design_features"), "design_features")
            if len(features) != len(STRUCTURE_FEATURE_NAMES) or any(
                not 0 <= v <= 1 for v in features
            ):
                raise InvalidRequestError("matched features must be six bounded descriptors")
            _number(c.payload.get("reward_posterior_mean"), "reward_posterior_mean")
            _number(c.payload.get("reward_posterior_std"), "reward_posterior_std", nonnegative=True)

    def create(
        self, candidates: Sequence[Candidate], metadata: Mapping[str, Any], seed: int | None
    ) -> StructuralKrigingBelieverSession:
        self.validate(candidates, metadata)
        return StructuralKrigingBelieverSession(candidates)


def experiment_sampling_request(
    strategy: str,
    candidates: Sequence[Candidate],
    *,
    reference_features: Mapping[str, Sequence[float]],
    budget: int,
    seed: int,
    design_size: int,
    offset: int,
) -> SamplingRequest:
    if strategy not in LITERATURE_BASELINE_STRATEGIES:
        raise InvalidRequestError(f"not a matched literature strategy: {strategy}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise InvalidRequestError("matched experiment requires an integer acquisition seed")
    normalized = []
    for c in candidates:
        # Deliberate whitelist: unselected targets, success masks, cost and metrics cannot leak in.
        features = _vector(c.payload.get("design_features"), "design_features")
        if len(features) != len(STRUCTURE_FEATURE_NAMES) or any(not 0 <= v <= 1 for v in features):
            raise InvalidRequestError("matched features must be six bounded descriptors")
        count = _number(c.payload.get("product_count"), "product_count")
        if count < 1 or not count.is_integer():
            raise InvalidRequestError(
                "product_count must be a positive integer from prior structures"
            )
        mean = _number(c.payload.get("prediction_mean"), "prediction_mean")
        predictive = _number(
            c.payload.get("predictive_variance"), "predictive_variance", nonnegative=True
        )
        epistemic = _number(
            c.payload.get("epistemic_variance"), "epistemic_variance", nonnegative=True
        )
        normalized.append(
            Candidate(
                c.candidate_id,
                {
                    "design_features": list(features),
                    "product_count": count,
                    "prediction_mean": mean,
                    "predictive_variance": predictive,
                    "epistemic_variance": epistemic,
                    "reward_posterior_mean": mean,
                    "reward_posterior_std": math.sqrt(epistemic) / count,
                },
            )
        )
    references = {
        str(key): list(_vector(value, "reference_features"))
        for key, value in reference_features.items()
    }
    if not references or set(references) & {c.candidate_id for c in candidates}:
        raise InvalidRequestError(
            "matched experiment requires disjoint selected reference features"
        )
    if any(
        len(v) != len(STRUCTURE_FEATURE_NAMES) or any(not 0 <= x <= 1 for x in v)
        for v in references.values()
    ):
        raise InvalidRequestError("reference features must use the same six bounded descriptors")
    if offset < 0 or design_size < offset + budget:
        raise InvalidRequestError("matched design_size must cover offset + budget")
    options: dict[str, Any]
    if strategy == "latin-hypercube":
        options = {
            "bounds": [[0, 1]] * len(STRUCTURE_FEATURE_NAMES),
            "design_size": design_size,
            "offset": offset,
        }
    elif strategy == "ucb":
        options = {"quantile": 0.95, "direction": "minimize"}
    elif strategy == "distance-uncertainty-product":
        options = {"reference_features": references, "variance_field": "predictive_variance"}
    elif strategy == "uncertainty-kriging-believer":
        options = {"length_scale": 1.0}
    else:
        options = {}
    common = {
        "schema": EXPERIMENT_SAMPLING_SCHEMA,
        "feature_names": list(STRUCTURE_FEATURE_NAMES),
        "feature_transform": "count/(1+count)",
        "predictive_aggregation": "sum of variances over structurally enumerated product routes",
        "reward_mean": "mean prediction over the same prior routes; lower energy preferred",
        "reward_variance": "sum(epistemic_variance)/product_count^2; independent-route approximation",
        "kb_correlation": "structural RBF length_scale=1; modeled correlation, not SAR joint posterior",
        "seed": seed,
        "design_size": design_size,
        "offset": offset,
        "shared_inputs_sha256": protocol_hash(
            {
                "candidates": sorted(
                    [c.to_dict() for c in normalized], key=lambda c: c["candidate_id"]
                ),
                "reference_features": references,
            }
        ),
    }
    return SamplingRequest(
        tuple(normalized),
        budget,
        strategy,
        seed,
        {"baseline": options, "sampling_experiment": common},
    )


def select_experiment_candidates(
    strategy: str,
    candidates: Sequence[Candidate],
    *,
    reference_features: Mapping[str, Sequence[float]],
    budget: int,
    seed: int,
    design_size: int,
    offset: int,
) -> SamplingResult:
    from .plugin import SamplingPlugin

    request = experiment_sampling_request(
        strategy,
        candidates,
        reference_features=reference_features,
        budget=budget,
        seed=seed,
        design_size=design_size,
        offset=offset,
    )
    return SamplingPlugin(kriging_believer_factory=StructuralKrigingBelieverFactory()).select(
        request
    )
