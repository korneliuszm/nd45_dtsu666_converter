"""Async CHINT e2TANGO Modbus TCP poller, aggregating several controllers.

Drives the etango bridge: an alternative source for the second bridge,
selectable in config.json alongside `huawei` (see `huawei_poller.py`).
`poll_once` is signature-compatible with `nd45_poller.poll_once`/
`huawei_poller.poll_once`, which is what lets `run_poller` drive it via
`poll_once_fn`.

Two things make e2TANGO different from the single-host sources:

- **Multiple independent TCP hosts, not one.** A site may run several e2TANGO
  protection relays (e.g. one per incoming feeder) that must be combined into
  a single canonical reading -- `MultiHostClient` holds one real Modbus TCP
  connection per configured device and this module reads/decodes each before
  combining.
- **Averaging vs summing across devices.** Voltages, currents, frequency and
  power factor are physical quantities that should agree across devices on
  the same bus (averaged to smooth measurement noise); powers and energies are
  additive across independent feeders (summed). Which applies to which
  canonical point is declared per-point/per-derive-rule in registers.json via
  `SourcePoint.aggregate`/`DeriveOp.aggregate`, not hardcoded here.

Unlike the SmartLogger, a bad reading from any one device fails the *whole*
sample (`PollError`), rather than zeroing just that channel -- deliberately
stricter, since a group of independent feeder meters silently losing one
member would understate the total by an unknown, unflagged amount.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from .canonical import apply_derive, compute_derived
from .codec import decode_point
from .config import SourceSide

log = logging.getLogger(__name__)


class PollError(RuntimeError):
    pass


@dataclass
class DeviceLink:
    """One configured e2TANGO controller: its client plus how to use it."""

    client: object  # AsyncModbusTcpClient, or a duck-typed fake in tests
    unit_id: int
    aggregate: bool
    host: str  # for error messages only


@dataclass
class MultiHostClient:
    """Facade over several `DeviceLink`s presenting the single-client
    interface `app.connect_with_retry`/`app.supervise_poller` expect
    (`connect()`, `close()`, `connected`), so no source-type branch is needed
    at those call sites.
    """

    devices: list[DeviceLink] = field(default_factory=list)

    async def connect(self) -> bool:
        """All-or-nothing: True only once every device is connected."""
        results = []
        for link in self.devices:
            try:
                results.append(bool(await link.client.connect()))
            except Exception:  # noqa: BLE001 - a single bad host must not crash startup
                log.warning("etango device %s failed to connect", link.host, exc_info=True)
                results.append(False)
        return all(results)

    def close(self) -> None:
        for link in self.devices:
            try:
                link.client.close()
            except Exception:  # noqa: BLE001 - best-effort, mirrors app._close_quietly
                log.debug("etango device %s client close() failed", link.host, exc_info=True)

    @property
    def connected(self) -> bool:
        return all(getattr(link.client, "connected", False) for link in self.devices)


def validate_source_coverage(source: SourceSide) -> None:
    """Require explicit read blocks; point coverage itself is checked at load.

    Same rationale as huawei_poller.validate_source_coverage: without
    `read_groups` a device would be polled for nothing and look exactly like
    it's dead.
    """
    if not source.read_groups:
        raise ValueError(
            "etango source must declare 'read_groups' in registers.json"
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
    client, source: SourceSide, unit_id: int, host: str = ""
) -> list[tuple[int, list[int]]]:
    """Read every block of one device, keyed by documented (pre-offset) base.

    e2TANGO's live-measurement table is served on function code 04 (Read Input
    Registers), unlike ND45/Huawei's function code 03. `host` is included in
    error messages only, to identify which of several controllers failed.
    """
    groups: list[tuple[int, list[int]]] = []
    for group in source.read_groups or []:
        unit = group.unit_id if group.unit_id is not None else unit_id
        wire_base = group.base + source.address_offset
        rr = await client.read_input_registers(wire_base, group.count, slave=unit)
        if rr.isError():
            raise PollError(
                f"e2TANGO device {host} read error at {wire_base} (unit {unit}): {rr}"
            )
        if len(rr.registers) < group.count:
            raise PollError(
                f"e2TANGO device {host} short read at {wire_base} (unit {unit}): "
                f"got {len(rr.registers)} of {group.count} registers"
            )
        groups.append((group.base, rr.registers))
    return groups


def decode_source(
    source: SourceSide, groups: list[tuple[int, list[int]]]
) -> dict[str, float]:
    """Decode one device's blocks into SI values (before derive).

    Every e2TANGO point is float32 -- unlike Huawei there are no sized
    integers in the live-measurement table.
    """
    wo, bo = source.word_order, source.byte_order
    values: dict[str, float] = {}
    for key, pt in source.points.items():
        regs = extract_registers(pt.addr, groups, pt.width)
        values[key] = decode_point(regs, pt.scale, pt.sign, pt.offset, wo, bo)
    return values


def _aggregate_mode(key: str, source: SourceSide) -> str:
    point = source.points.get(key)
    if point is not None:
        return point.aggregate
    for op in source.derive:
        if key in op.targets:
            return op.aggregate
    # Reached only for a key neither a point nor a derive target produces,
    # which the register-map validators already reject at load time.
    return "avg"  # pragma: no cover - defensive, unreachable via a loaded map


def _combine(per_device: list[dict[str, float]], source: SourceSide) -> dict[str, float]:
    """Average or sum each canonical key across the aggregating devices."""
    keys: set[str] = set()
    for values in per_device:
        keys.update(values)
    combined: dict[str, float] = {}
    for key in keys:
        readings = [values[key] for values in per_device if key in values]
        total = math.fsum(readings)
        combined[key] = total if _aggregate_mode(key, source) == "sum" else total / len(readings)
    return combined


async def poll_once(
    client,  # MultiHostClient
    source: SourceSide,
    slave: int,  # unused: each device carries its own unit_id (DeviceLink.unit_id);
    # kept only so app.supervise_poller's generic call (passing
    # spec.source.unit_id as `slave` for every source type) stays branch-free.
    overrange_seen: set[str] | None = None,
) -> dict[str, float]:
    """Read, decode and combine every configured e2TANGO controller.

    Every configured device is read every cycle, regardless of its
    `aggregate` flag, so a device excluded from the combined reading can
    still fail the whole sample on a read error -- a device that should not
    be able to do that must be removed from `devices`, not merely flagged
    `aggregate: false`.

    Unlike the SmartLogger, nothing is silently zeroed: a read error, short
    read, or non-finite decoded/derived value on ANY device raises
    `PollError` for the whole sample, so `overrange_seen` (accepted only for
    signature compatibility with `run_poller`) is never populated here.

    Per-device `derive` rules (e.g. per-phase apparent power/power factor)
    run BEFORE combination, not after: those ops are non-linear (hypot,
    p/s), so deriving from each device's own readings and then summing/
    averaging is physically correct, while combining raw inputs first and
    deriving once would generally give a different, wrong answer.
    """
    per_device: list[dict[str, float]] = []
    for link in client.devices:
        groups = await read_groups(link.client, source, link.unit_id, host=link.host)
        values = decode_source(source, groups)
        apply_derive(values, source.derive)
        for key, value in values.items():
            if not math.isfinite(value):
                raise PollError(
                    f"e2TANGO device {link.host} produced a non-finite value "
                    f"for {key!r}: {value!r}"
                )
        per_device.append(values)

    aggregating = [
        values
        for values, link in zip(per_device, client.devices)
        if link.aggregate
    ]
    combined = _combine(aggregating, source)

    # This bridge owns a full canonical model of its own, so it fills the
    # energy aliases the DTSU maps reference exactly as the ND45/Huawei
    # pollers do, on the already-combined totals.
    compute_derived(combined)
    return combined
