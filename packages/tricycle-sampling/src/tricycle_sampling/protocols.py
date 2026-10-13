"""Versioned literature contracts; citations describe use, not algorithm invention."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from tricycle_core.errors import InvalidRequestError


@dataclass(frozen=True, slots=True)
class SamplingBaselineProtocol:
    strategy: str
    protocol_id: str
    version: str
    authors: str
    title: str
    journal: str
    year: int
    doi: str
    evidence: str
    rule: str
    adaptation: str

    def to_dict(self) -> dict[str, Any]:
        record = asdict(self)
        record["url"] = f"https://doi.org/{self.doi}"
        record["tie_break"] = "candidate_id ascending"
        record["sha256"] = protocol_hash(record)
        return record


def protocol_hash(record: Any) -> str:
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


LITERATURE_BASELINES: tuple[SamplingBaselineProtocol, ...] = (
    SamplingBaselineProtocol(
        "predictive-uncertainty",
        "tricycle.sampling.predictive-uncertainty/v1",
        "1.0.0",
        "Zhong et al.",
        "Towards global reaction feasibility and robustness prediction with high throughput "
        "data and bayesian deep learning",
        "Nature Communications",
        2025,
        "10.1038/s41467-025-59812-0",
        "Fine-grained Uncertainty Disentanglement; Methods; Fig. 4e–g",
        "Descending predictive uncertainty; Bernoulli posterior entropy when probabilities supplied.",
        "RxnResid variance/std is a regression adaptation of the classification entropy rule; "
        "existing score-field precedence is retained. Only predictive represents this paper in the profile.",
    ),
    SamplingBaselineProtocol(
        "latin-hypercube",
        "tricycle.sampling.latin-hypercube/v1",
        "1.0.0",
        "Slattery et al.",
        "Automated self-optimization, intensification, and scale-up of photocatalysis in flow",
        "Science",
        2024,
        "10.1126/science.adj1817",
        "Model-based optimization; Materials and methods / Platform operation",
        "Seeded randomized LHS in explicit fixed bounds; greedy nearest unused pool candidate.",
        "The paper uses LHS initialization. Discrete nearest-neighbor mapping and prefix batching "
        "are TriCycle adaptations, not a claim that mapped points remain an exact Latin hypercube.",
    ),
    SamplingBaselineProtocol(
        "ucb",
        "tricycle.sampling.bayes-ucb-gaussian/v1",
        "1.0.0",
        "Wang et al.",
        "Identifying general reaction conditions by bandit optimization",
        "Nature",
        2024,
        "10.1038/s41586-024-07021-y",
        "Model design and development; Fig. 2; Supplementary Information Section 8",
        "Rank the Gaussian reward posterior upper quantile, with an explicit reward direction.",
        "Continuous Gaussian Bayes-UCB family only. Fixed quantile and independent candidate "
        "arms are pool adaptations; upstream supplies updated reward posteriors. Predictive "
        "variance is not silently substituted for uncertainty of an arm's expected reward.",
    ),
    SamplingBaselineProtocol(
        "distance-uncertainty-product",
        "tricycle.sampling.distance-uncertainty-product/v1",
        "1.0.0",
        "Shrestha et al.",
        "Machine intelligence accelerated design of conductive MXene aerogels with programmable properties",
        "Nature Communications",
        2024,
        "10.1038/s41467-024-49011-8",
        "Constructing a prediction model via active learning loops; Eq. (1); Supplementary Note 7",
        "Nearest labeled-reference Euclidean distance times an explicitly chosen variance field; top-k.",
        "Nearest-reference aggregation, frozen feature scaling, and pair-level RxnResid variance "
        "are declared adaptations. No normalization or within-batch distance update is implied "
        "by the paper's hat symbols; this is not the existing additive uncertainty-diversity rule.",
    ),
    SamplingBaselineProtocol(
        "uncertainty-kriging-believer",
        "tricycle.sampling.uncertainty-kriging-believer/v1",
        "1.0.0",
        "Souza et al.",
        "Applying Active Learning toward Building a Generalizable Model for Ni-Photoredox "
        "Cross-Electrophile Coupling of Aryl and Alkyl Bromides",
        "Journal of the American Chemical Society",
        2025,
        "10.1021/jacs.5c02218",
        "Batch Selection; Fig. 4–6",
        "Sequential max standard deviation; condition/refit on the selected prediction as a "
        "temporary label before choosing the next candidate.",
        "Original RF/tree standard deviation and building-block aggregation are not reproduced "
        "by the default adapter: TriCycle uses pair-level Gaussian joint covariance conditioning. "
        "A local refitting surrogate may be injected. Pseudo-labels never become permanent labels.",
    ),
)

LITERATURE_BASELINE_STRATEGIES = tuple(item.strategy for item in LITERATURE_BASELINES)


def get_baseline_protocol(strategy: str) -> SamplingBaselineProtocol:
    for item in LITERATURE_BASELINES:
        if item.strategy == strategy:
            return item
    raise InvalidRequestError(f"strategy has no literature baseline protocol: {strategy}")
