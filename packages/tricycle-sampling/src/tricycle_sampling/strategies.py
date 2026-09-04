from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Protocol

from .models import Candidate


class SamplingStrategy(Protocol):
    name: str

    def select(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
    ) -> tuple[Candidate, ...]: ...


class RandomSamplingStrategy:
    name = "random"

    def select(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
    ) -> tuple[Candidate, ...]:
        generator = random.Random(seed)
        return tuple(generator.sample(list(candidates), budget))


class FirstNSamplingStrategy:
    name = "first-n"

    def select(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
    ) -> tuple[Candidate, ...]:
        del seed
        return tuple(candidates[:budget])
