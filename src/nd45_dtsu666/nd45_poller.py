"""Async ND45 Modbus TCP poller: read register blocks, decode into SI values."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .codec import OVERRANGE, compose, decode_point, registers_to_float
from .config import SourceSide

log = logging.getLogger(__name__)

# Fixed read blocks (base_addr, register_count) covering every mapped ND45 point.
# Group 1: 200 ms measurements 50..145; Group 2: frequency 818..819; Group 3: energy 900..931.
READ_GROUPS: list[tuple[int, int]] = [(50, 96), (818, 2), (900, 32)]


class PollError(RuntimeError):
    pass


def extract_registers(addr: int, groups: list[tuple[int, list[int]]]) -> list[int]:
    for base, regs in groups:
        if base <= addr < base + len(regs) - 1:
            off = addr - base
            return regs[off:off + 2]
    raise KeyError(addr)


async def poll_once(client, source: SourceSide, slave: int) -> dict[str, float]:
    groups: list[tuple[int, list[int]]] = []
    for base, count in READ_GROUPS:
        rr = await client.read_holding_registers(base, count, slave=slave)
        if rr.isError():
            raise PollError(f"ND45 read error at {base}: {rr}")
        groups.append((base, rr.registers))

    wo, bo = source.word_order, source.byte_order
    values: dict[str, float] = {}
    for key, pt in source.points.items():
        if pt.compose:
            parts = [registers_to_float(extract_registers(a, groups), wo, bo) for a in pt.compose]
            si = compose(parts, pt.factors or [1.0] * len(parts))
        else:
            regs = extract_registers(pt.addr, groups)
            si = decode_point(regs, pt.scale, pt.sign, pt.offset, wo, bo)
        if abs(si) >= OVERRANGE:
            log.warning("ND45 %s over range, using 0.0", key)
            si = 0.0
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
    while not stop_event.is_set():
        try:
            values = await poll_once(client, source, slave)
            on_update(values, loop.time())
        except Exception as exc:  # noqa: BLE001 - poller must never die
            on_error(exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
