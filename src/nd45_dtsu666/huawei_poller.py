"""Async Huawei SmartLogger Modbus TCP poller.

Second data source alongside the ND45, added so the bridge can report the PV
farm's instantaneous production. Differs from `nd45_poller` in three ways that
are all forced by the device:

- **Sized integers, not float32.** Every SmartLogger register is U16/I16/U32/
  I32/U64/I64 with a documented "Gain" divisor, which folds into the map's
  existing `scale` multiplier (see `codec.decode_int_point`).
- **Several unit ids per poll.** The SmartLogger answers for its own aggregated
  plant registers as logic device 0, and for each attached device (here: a power
  meter) as that device's RS485 address. One TCP client covers both by varying
  `slave=` per read block.
- **Config-driven read blocks.** Blocks come from `SourceSide.read_groups` in
  registers.json instead of a module constant, because two different register
  regions (0x7E04-ish plant, 0x7E04-ish meter) are involved and their layout is
  an on-site finding, not a code constant.

Coverage is deliberately partial: no SmartLogger register set covers the whole
canonical model, and grid frequency appears nowhere in the manual at all. The
gaps are filled by the declarative `derive` rules the map carries
(`canonical.apply_derive`).
"""

from __future__ import annotations

import logging
import math

from .canonical import apply_derive
from .codec import decode_int_point, decode_point
from .config import AppConfig, RegisterMap, SourceSide

log = logging.getLogger(__name__)


class PollError(RuntimeError):
    pass


def validate_source_coverage(source: SourceSide) -> None:
    """Require explicit read blocks; point coverage itself is checked at load.

    `SourceSide` validates that every point falls inside a block when
    `read_groups` is set, so the only thing left to reject here is a Huawei
    source that forgot to declare blocks at all (which would poll nothing and
    look like a dead SmartLogger).
    """
    if not source.read_groups:
        raise ValueError("huawei source must declare 'read_groups' in registers.json")


def select_sources(
    config: AppConfig, registers: RegisterMap
) -> list[tuple[str, SourceSide, int]]:
    """Resolve the configured SmartLogger sources to (name, side, unit id).

    Raises rather than silently polling nothing, so a half-finished config fails
    at startup instead of presenting as a permanently stale PV panel.
    """
    wanted = [
        ("huawei_plant", config.huawei.plant_unit_id, registers.huawei_plant_source),
        ("huawei_meter", config.huawei.meter_unit_id, registers.huawei_meter_source),
    ]
    selected: list[tuple[str, SourceSide, int]] = []
    for name, unit_id, side in wanted:
        if unit_id is None:
            continue
        if side is None:
            raise ValueError(
                f"huawei.{name.removeprefix('huawei_')}_unit_id is set but "
                f"registers.json has no {name}_source section"
            )
        validate_source_coverage(side)
        selected.append((name, side, unit_id))
    if not selected:
        raise ValueError("huawei.enabled is set but no unit id selects a source")
    return selected


def extract_registers(
    addr: int, groups: list[tuple[int, list[int]]], width: int
) -> list[int]:
    """Slice `width` registers starting at `addr` out of the read blocks."""
    for base, regs in groups:
        offset = addr - base
        if 0 <= offset and offset + width <= len(regs):
            return regs[offset:offset + width]
    raise KeyError(addr)


async def read_groups(client, source: SourceSide, slave: int) -> list[tuple[int, list[int]]]:
    """Read every block of one source, keyed by documented (pre-offset) base."""
    groups: list[tuple[int, list[int]]] = []
    for group in source.read_groups or []:
        unit = group.unit_id if group.unit_id is not None else slave
        wire_base = group.base + source.address_offset
        rr = await client.read_holding_registers(wire_base, group.count, slave=unit)
        if rr.isError():
            raise PollError(f"SmartLogger read error at {wire_base} (unit {unit}): {rr}")
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
    """Decode one source's blocks into SI values (before derive)."""
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
    sources: list[tuple[str, SourceSide, int]],
    slave: int = 0,
    overrange_seen: set[str] | None = None,
) -> dict[str, float]:
    """Read and decode every configured SmartLogger source into canonical SI.

    Signature mirrors `nd45_poller.poll_once` so `run_poller` can drive it via
    `poll_once_fn`; `slave` is only the fallback unit id for a block that does
    not name its own.

    Unlike the ND45 path, an invalid channel never rejects the whole sample. The
    SmartLogger reports the type-max sentinel for anything it cannot currently
    supply (a disconnected inverter, an unpopulated register), and this source is
    telemetry that must not gate the DTSU output -- so a bad point is zeroed and
    logged once per episode, and the rest of the poll still lands.
    """
    values: dict[str, float] = {}
    for _name, source, unit_id in sources:
        groups = await read_groups(client, source, unit_id if unit_id is not None else slave)
        values.update(decode_source(source, groups))

    for key, value in list(values.items()):
        if math.isfinite(value):
            if overrange_seen is not None and key in overrange_seen:
                overrange_seen.discard(key)
                log.info("SmartLogger %s back in range", key)
            continue
        if overrange_seen is None or key not in overrange_seen:
            log.warning("SmartLogger %s invalid/unavailable (%r), using 0.0", key, value)
            if overrange_seen is not None:
                overrange_seen.add(key)
        values[key] = 0.0

    for _name, source, _unit_id in sources:
        apply_derive(values, source.derive)

    # Derive rules can still produce NaN from a zeroed input (e.g. a ratio that
    # loses its weights); scrub once more so nothing non-finite reaches the
    # store, where the output encoder would raise on it.
    for key, value in list(values.items()):
        if not math.isfinite(value):
            log.warning("SmartLogger derived %s is not finite (%r), using 0.0", key, value)
            values[key] = 0.0

    # Deliberately NOT calling canonical.compute_derived here. It reads the
    # unprefixed imp_energy_total/exp_energy_total, which belong to the primary
    # (ND45) source -- a SmartLogger map uses prefixed names, so it would write
    # active_energy_total/net_*_energy_total as zeros over real meter energy.
    # A map that really wants to feed those from Huawei declares explicit derive
    # rules for them.
    return values
