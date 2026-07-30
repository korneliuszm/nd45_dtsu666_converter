"""In-memory canonical SI store with a freshness gate for fail-safe."""

from __future__ import annotations

import math

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


class MergedStore:
    """Union of several per-source stores; freshness follows the primary only.

    Drop-in for CanonicalStore at every read site (`supervise_server`, metrics,
    monitor all use only `age`/`snapshot`/`is_fresh`).

    The primary-only freshness rule is the safety property of this class. The
    DTSU output is silenced when primary (ND45) data goes stale, because that is
    the measurement Sigenergy regulates on. A secondary source is telemetry --
    the SmartLogger's PV production refreshes far more slowly and may drop out
    for minutes -- and must never be able to silence a healthy bridge.
    """

    def __init__(
        self, primary: CanonicalStore, secondaries: dict[str, CanonicalStore] | None = None
    ) -> None:
        self.primary = primary
        self.secondaries = dict(secondaries or {})

    def snapshot(self) -> tuple[dict[str, float], float]:
        values: dict[str, float] = {}
        for store in self.secondaries.values():
            values.update(store.snapshot()[0])
        # primary last: its keys win any collision, so a stray secondary point
        # name can never displace a grid-tie measurement.
        primary_values, ts = self.primary.snapshot()
        values.update(primary_values)
        return values, ts

    def age(self, now: float) -> float:
        return self.primary.age(now)

    def is_fresh(self, now: float, max_age: float) -> bool:
        return self.primary.is_fresh(now, max_age)

    def source_age(self, name: str, now: float) -> float:
        """Age of one secondary source, for metrics and the dashboard."""
        store = self.secondaries.get(name)
        return math.inf if store is None else store.age(now)


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


class HealthGate:
    def __init__(self, max_age: float) -> None:
        self.max_age = max_age

    def should_serve(self, age: float) -> bool:
        return age <= self.max_age
