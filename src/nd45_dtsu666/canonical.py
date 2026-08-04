"""In-memory canonical SI store with a freshness gate for fail-safe."""

from __future__ import annotations

import math
from collections import deque

from .config import DeriveOp

SQRT3 = math.sqrt(3.0)


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


def compute_derived(values: dict[str, float]) -> None:
    """Fill canonical physical-meter energy aliases in place.

    Lives here rather than in a source driver: every source (ND45 poller,
    static debug, diagnostics, Huawei poller) must produce these, since
    `dtsu_target` and `dtsu_sigen_ext_energy` reference them via `from` and
    `update_datastore` silently skips a missing canonical key.
    """
    imp = values.get("imp_energy_total", 0.0)
    exp = values.get("exp_energy_total", 0.0)
    values["active_energy_total"] = imp + exp
    values["net_imp_energy_total"] = imp
    values["net_exp_energy_total"] = exp


def _get(values: dict[str, float], name: str) -> float:
    value = values.get(name)
    return math.nan if value is None else value


def apply_derive(values: dict[str, float], ops: list[DeriveOp]) -> None:
    """Run declarative derive steps in order, writing results into `values`.

    Exists because no Huawei SmartLogger register set covers the full canonical
    model: per-phase reactive/apparent power, phase voltages and grid frequency
    are simply absent (frequency appears nowhere in the manual). Keeping the
    rules in registers.json means they can be corrected during on-site bring-up
    without shipping code -- the same principle as the register maps themselves.

    A step whose inputs are missing or non-finite writes NaN, which the caller's
    validity check then rejects, rather than silently inventing a number.
    """
    for op in ops:
        if op.op == "constant":
            for target in op.targets:
                values[target] = float(op.value)  # validated non-None and finite
        elif op.op == "copy":
            source = _get(values, op.from_[0])
            for target in op.targets:
                values[target] = source
        elif op.op == "split_equal":
            share = _get(values, op.from_[0]) / len(op.targets)
            for target in op.targets:
                values[target] = share
        elif op.op == "phase_from_line":
            for target, line in zip(op.targets, op.from_):
                values[target] = _get(values, line) / SQRT3
        elif op.op == "hypot":
            for index, target in enumerate(op.targets):
                a = _get(values, op.from_[2 * index])
                b = _get(values, op.from_[2 * index + 1])
                values[target] = math.hypot(a, b)
        elif op.op == "ratio_split":
            _apply_ratio_split(values, op)
        elif op.op == "pf_from_p_s":
            for index, target in enumerate(op.targets):
                p = _get(values, op.from_[2 * index])
                s = _get(values, op.from_[2 * index + 1])
                # A meter at rest reports S = 0; unity is the convention the
                # DTSU666 itself shows there, and 0/0 would poison the sample.
                values[target] = 1.0 if s == 0.0 else p / s
        else:  # pragma: no cover - DeriveOp.op is a validated Literal
            raise ValueError(f"unknown derive op {op.op!r}")


def _apply_ratio_split(values: dict[str, float], op: DeriveOp) -> None:
    """Split a total across targets in proportion to named weight points.

    Used for per-phase reactive power, which the Huawei power-meter table omits
    while giving per-phase active power: apportioning Q by each phase's share of
    P is closer to a real unbalanced load than an equal split. Falls back to an
    equal split when the weights sum to ~0 (night-time, no production), where
    the proportions carry no information.
    """
    total = _get(values, op.from_[0])
    weights = [_get(values, name) for name in op.weights]
    weight_sum = math.fsum(abs(w) for w in weights)
    if not math.isfinite(weight_sum) or weight_sum == 0.0:
        share = total / len(op.targets)
        for target in op.targets:
            values[target] = share
        return
    for target, weight in zip(op.targets, weights):
        values[target] = total * abs(weight) / weight_sum


class SampleStats:
    """Spread of recent canonical samples, per point.

    A single instantaneous reading cannot answer "is this value jittering?", and
    a refreshing screen is a bad instrument -- the same signal reads differently
    at 1s and at 0.3s, because sampling slower than the source's own measurement
    cycle aliases the swing away instead of removing it. Min/max and the number
    of zero crossings make that comparable as numbers.

    `window` bounds the history: the monitor wants the last few seconds, diag a
    whole bring-up session.
    """

    def __init__(self, points: tuple[str, ...], window: int = 30) -> None:
        self._points = points
        self._history: dict[str, deque[float]] = {
            name: deque(maxlen=window) for name in points
        }
        self.samples = 0

    def record(self, values: dict[str, float]) -> None:
        self.samples += 1
        for name in self._points:
            value = values.get(name)
            if value is not None and math.isfinite(value):
                self._history[name].append(value)

    def summary(self, name: str) -> dict | None:
        """min/max/last/sign_changes for one point, or None if it never arrived."""
        history = self._history.get(name)
        if not history:
            return None
        crossings, last_signed = 0, None
        for value in history:
            # Compared against the last *signed* sample: a walk 5 -> 0 -> -5
            # crossed zero once, and comparing with the 0.0 in between scores none.
            if last_signed is not None and value * last_signed < 0:
                crossings += 1
            if value != 0.0:
                last_signed = value
        return {
            "n": len(history), "min": min(history), "max": max(history),
            "last": history[-1], "sign_changes": crossings,
        }

    def render(self, interval: float) -> str:
        lines = [
            "",
            f"spread over {self.samples} sample(s) at {interval:g}s "
            f"({self.samples * interval:.0f}s of history)",
            f"{'point':<12}{'last':>14}{'min':>14}{'max':>14}"
            f"{'peak-peak':>14}{'sign flips':>12}",
            "-" * 80,
        ]
        for name in self._points:
            stat = self.summary(name)
            if stat is None:
                lines.append(f"{name:<12}{'-':>14}")
                continue
            lines.append(
                f"{name:<12}{stat['last']:>14.3f}{stat['min']:>14.3f}"
                f"{stat['max']:>14.3f}{stat['max'] - stat['min']:>14.3f}"
                f"{int(stat['sign_changes']):>12}"
            )
        return "\n".join(lines)


class HealthGate:
    def __init__(self, max_age: float) -> None:
        self.max_age = max_age

    def should_serve(self, age: float) -> bool:
        return age <= self.max_age
