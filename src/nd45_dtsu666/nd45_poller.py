"""Async ND45 Modbus TCP poller: read register blocks, decode into SI values."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable

from .codec import OVERRANGE, compose, decode_point, registers_to_float
from .config import SourceSide

log = logging.getLogger(__name__)

# Fixed read blocks (base_addr, register_count) covering every mapped ND45 point.
# Group 1: 200 ms measurements 50..145; Group 2: frequency 818..819; Group 3: energy 900..931.
READ_GROUPS: list[tuple[int, int]] = [(50, 96), (818, 2), (900, 32)]


class PollError(RuntimeError):
    pass


def _source_point_addresses(source: SourceSide) -> set[int]:
    addrs: set[int] = set()
    for pt in source.points.values():
        if pt.compose:
            addrs.update(pt.compose)
        elif pt.addr is not None:
            addrs.add(pt.addr)
    return addrs


def validate_source_coverage(source: SourceSide) -> None:
    """Fail loudly at startup if any nd45_source address can't be read.

    Every point is a float32 (2 registers) served from the fixed READ_GROUPS.
    An address outside them makes poll_once raise KeyError on every poll, which
    the fault reporter mutes -- leaving the bridge in a permanent fail-safe that
    is indistinguishable from a real ND45 outage. Catch it at load time instead,
    when registers.json is edited (this project's maps change without code).
    """
    uncovered = [
        addr
        for addr in sorted(_source_point_addresses(source))
        if not any(base <= addr < base + count - 1 for base, count in READ_GROUPS)
    ]
    if uncovered:
        raise ValueError(
            f"nd45_source addresses {uncovered} are not covered by READ_GROUPS "
            f"{READ_GROUPS}; extend READ_GROUPS in nd45_poller.py to include them"
        )


def extract_registers(addr: int, groups: list[tuple[int, list[int]]]) -> list[int]:
    for base, regs in groups:
        if base <= addr < base + len(regs) - 1:
            off = addr - base
            return regs[off:off + 2]
    raise KeyError(addr)


async def poll_once(
    client, source: SourceSide, slave: int, overrange_seen: set[str] | None = None
) -> dict[str, float]:
    """Read all groups and decode into canonical SI values.

    `overrange_seen` (owned by the caller, e.g. run_poller) mutes repeated
    over-range warnings: a sustained bad channel logs once per episode, not
    once per 0.3s poll (~288k journal lines/day otherwise).
    """
    groups: list[tuple[int, list[int]]] = []
    for base, count in READ_GROUPS:
        rr = await client.read_holding_registers(base, count, slave=slave)
        if rr.isError():
            raise PollError(f"ND45 read error at {base}: {rr}")
        # A short frame would otherwise surface as a cryptic KeyError from
        # extract_registers for whichever point falls in the missing tail;
        # name it explicitly so the log points at the real cause.
        if len(rr.registers) < count:
            raise PollError(
                f"ND45 short read at {base}: got {len(rr.registers)} of {count} registers"
            )
        groups.append((base, rr.registers))

    wo, bo = source.word_order, source.byte_order
    values: dict[str, float] = {}
    for key, pt in source.points.items():
        if pt.compose:
            parts = [registers_to_float(extract_registers(a, groups), wo, bo) for a in pt.compose]
            raw = compose(parts, pt.factors or [1.0] * len(parts))
            si = raw * pt.scale * pt.sign + pt.offset
        else:
            regs = extract_registers(pt.addr, groups)
            si = decode_point(regs, pt.scale, pt.sign, pt.offset, wo, bo)
        # NaN fails every comparison, so check finiteness explicitly -- a NaN
        # reading (e.g. PF undefined at ~0 A) must not reach Sigenergy.
        if not math.isfinite(si) or abs(si) >= OVERRANGE:
            if overrange_seen is None or key not in overrange_seen:
                log.warning("ND45 %s over range/invalid (%r), using 0.0", key, si)
                if overrange_seen is not None:
                    overrange_seen.add(key)
            si = 0.0
        elif overrange_seen is not None and key in overrange_seen:
            overrange_seen.discard(key)
            log.info("ND45 %s back in range", key)
        values[key] = si

    imp_total = values.get("imp_energy_total", 0.0)
    exp_total = values.get("exp_energy_total", 0.0)
    values["net_imp_energy_total"] = max(imp_total - exp_total, 0.0)
    values["net_exp_energy_total"] = max(exp_total - imp_total, 0.0)
    return values


async def run_poller(
    client,
    source: SourceSide,
    slave: int,
    interval: float,
    on_update: Callable[[dict[str, float], float], None],
    on_error: Callable[[Exception], None],
    stop_event: asyncio.Event,
) -> None:
    loop = asyncio.get_running_loop()
    overrange_seen: set[str] = set()
    while not stop_event.is_set():
        try:
            values = await poll_once(client, source, slave, overrange_seen=overrange_seen)
            on_update(values, loop.time())
        except Exception as exc:  # noqa: BLE001 - poller must never die
            try:
                on_error(exc)
            except Exception:  # noqa: BLE001 - the error handler must never kill the poller either
                log.exception("on_error callback raised; original poll failure was: %r", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
