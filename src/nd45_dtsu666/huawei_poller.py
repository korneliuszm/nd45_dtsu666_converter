"""Async Huawei SmartLogger Modbus TCP poller.

Drives the SmartLogger bridge, the sibling of the ND45 bridge: it produces the
same canonical SI model, so it feeds an independent DTSU666 output of its own.
`poll_once` is signature-compatible with `nd45_poller.poll_once`, which is what
lets `nd45_poller.run_poller` drive either one.

Three things differ, all forced by the device:

- **Sized integers, not float32.** Every SmartLogger register is U16/I16/U32/
  I32/U64/I64 with a documented "Gain" divisor, which folds into the map's
  existing `scale` multiplier (see `codec.decode_int_point`).
- **Config-driven read blocks.** Blocks come from `SourceSide.read_groups` in
  registers.json rather than a module constant, because which region is read
  depends on which device the SmartLogger is answering for -- its own aggregated
  plant registers (logic device 0) or an attached power meter (that device's
  RS485 address).
- **Partial coverage.** No SmartLogger register set covers the whole canonical
  model, and grid frequency appears nowhere in the manual at all. The gaps are
  filled by the declarative `derive` rules the map carries
  (`canonical.apply_derive`).
"""

from __future__ import annotations

import logging
import math

from .canonical import apply_derive, compute_derived
from .codec import decode_int_point, decode_point
from .config import SourceSide

log = logging.getLogger(__name__)


class PollError(RuntimeError):
    pass


def validate_source_coverage(source: SourceSide) -> None:
    """Require explicit read blocks; point coverage itself is checked at load.

    `SourceSide` validates that every point falls inside a block when
    `read_groups` is set, so the only thing left to reject here is a SmartLogger
    source that forgot to declare blocks at all -- which would poll nothing and
    look exactly like a dead device.
    """
    if not source.read_groups:
        raise ValueError(
            "huawei source must declare 'read_groups' in registers.json"
        )


def extract_registers(
    addr: int, groups: list[tuple[int, list[int]]], width: int
) -> list[int]:
    """Slice `width` registers starting at `addr` out of the read blocks."""
    for base, regs in groups:
        offset = addr - base
        if 0 <= offset and offset + width <= len(regs):
            return regs[offset:offset + width]
    raise KeyError(addr)


async def read_groups(
    client, source: SourceSide, slave: int
) -> list[tuple[int, list[int]]]:
    """Read every block of the source, keyed by documented (pre-offset) base."""
    groups: list[tuple[int, list[int]]] = []
    for group in source.read_groups or []:
        unit = group.unit_id if group.unit_id is not None else slave
        wire_base = group.base + source.address_offset
        rr = await client.read_holding_registers(wire_base, group.count, slave=unit)
        if rr.isError():
            raise PollError(
                f"SmartLogger read error at {wire_base} (unit {unit}): {rr}"
            )
        if len(rr.registers) < group.count:
            raise PollError(
                f"SmartLogger short read at {wire_base} (unit {unit}): "
                f"got {len(rr.registers)} of {group.count} registers"
            )
        groups.append((group.base, rr.registers))
    return groups


def decode_source(
    source: SourceSide, groups: list[tuple[int, list[int]]]
) -> dict[str, float]:
    """Decode the source's blocks into SI values (before derive)."""
    wo, bo = source.word_order, source.byte_order
    values: dict[str, float] = {}
    for key, pt in source.points.items():
        regs = extract_registers(pt.addr, groups, pt.width)
        if pt.dtype == "float32":
            values[key] = decode_point(regs, pt.scale, pt.sign, pt.offset, wo, bo)
        else:
            values[key] = decode_int_point(
                regs, pt.dtype, pt.scale, pt.sign, pt.offset, wo, bo
            )
    return values


async def poll_once(
    client,
    source: SourceSide,
    slave: int,
    overrange_seen: set[str] | None = None,
) -> dict[str, float]:
    """Read and decode the SmartLogger into canonical SI values.

    Signature mirrors `nd45_poller.poll_once` so `run_poller` drives either via
    `poll_once_fn`.

    Unlike the ND45 path, an invalid channel does not reject the whole sample.
    The SmartLogger reports the type-max sentinel for anything it cannot supply
    right now (a disconnected inverter, an unpopulated register), and losing an
    entire poll over one such point would stall a bridge whose source already
    refreshes slowly. A bad point is zeroed and logged once per episode instead.
    A source that is genuinely unreachable still raises from `read_groups`, so
    the store goes stale and this bridge's output is silenced as it should be.

    `overrange_seen` (owned by the caller, e.g. run_poller) mutes repeated
    warnings so a sustained bad channel logs once per episode, not once per poll.
    """
    groups = await read_groups(client, source, slave)
    values = decode_source(source, groups)

    for key, value in list(values.items()):
        if math.isfinite(value):
            if overrange_seen is not None and key in overrange_seen:
                overrange_seen.discard(key)
                log.info("SmartLogger %s back in range", key)
            continue
        if overrange_seen is None or key not in overrange_seen:
            log.warning(
                "SmartLogger %s invalid/unavailable (%r), using 0.0", key, value
            )
            if overrange_seen is not None:
                overrange_seen.add(key)
        values[key] = 0.0

    apply_derive(values, source.derive)

    # Derive rules can still produce NaN from a zeroed input (e.g. a ratio that
    # lost its weights); scrub once more so nothing non-finite reaches the store,
    # where the output encoder would raise on it.
    for key, value in list(values.items()):
        if not math.isfinite(value):
            log.warning(
                "SmartLogger derived %s is not finite (%r), using 0.0", key, value
            )
            values[key] = 0.0

    # This bridge owns a full canonical model of its own, so it fills the energy
    # aliases the DTSU maps reference exactly as the ND45 poller does.
    compute_derived(values)
    return values
