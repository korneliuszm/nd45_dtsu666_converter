"""In-memory canonical SI store with a freshness gate for fail-safe."""

from __future__ import annotations

import math


class CanonicalStore:
    def __init__(self) -> None:
        self._values: dict[str, float] = {}
        self._ts: float | None = None

    def update(self, values: dict[str, float], ts: float) -> None:
        self._values = dict(values)
        self._ts = ts

    def snapshot(self) -> tuple[dict[str, float], float]:
        return dict(self._values), (self._ts if self._ts is not None else math.nan)

    def age(self, now: float) -> float:
        if self._ts is None:
            return math.inf
        return now - self._ts

    def is_fresh(self, now: float, max_age: float) -> bool:
        return self.age(now) <= max_age


class HealthGate:
    def __init__(self, max_age: float) -> None:
        self.max_age = max_age

    def should_serve(self, age: float) -> bool:
        return age <= self.max_age
