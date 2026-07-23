"""Debug mode: log every Modbus register block the Sigenergy master reads over RTU.

This is a standalone bring-up / debugging mode, separate from `run`. It runs the
full bridge pipeline but injects a logging recorder into the DTSU output server's
read path (the existing `activity=` seam used by `monitor`), so each read request
from Sigenergy is printed with the DTSU register name(s) it touches.

The production `run` path passes no `activity` and is therefore unaffected.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from .app import build_pipeline, connect_with_retry
from .config import (
    AppConfig,
    RegisterMap,
    StaticIdentitySide,
    StaticZeroSide,
    TargetSide,
)
from .dtsu_server import _IDENTITY_REGISTER_ADDRS, RtuActivity

log = logging.getLogger("nd45_dtsu666.rtudebug")

# DTSU measurement points are float32 -> 2 holding registers each. The identity/
# config registers (see dtsu_server) are signed int16 -> 1 register each.
_MEASUREMENT_WORDS = 2
_IDENTITY_WORDS = 1

# Extra single-word identity registers written by write_static_registers but not
# present in _IDENTITY_REGISTER_ADDRS.
_EXTRA_IDENTITY_ADDRS: dict[str, int] = {"baud": 0x002D, "addr": 0x002E}


class RegisterNameIndex:
    """Maps a queried Modbus block (address, count) to overlapping register names."""

    def __init__(self, spans: list[tuple[int, int, int, str]]) -> None:
        # spans: (function_code, start_addr, word_len, name).
        self._spans = sorted(spans, key=lambda span: (span[0], span[1]))

    @classmethod
    def build(
        cls,
        target: TargetSide | Iterable[TargetSide],
        sigen_identity: StaticIdentitySide | None = None,
        sigen_zero_ranges: StaticZeroSide | None = None,
    ) -> "RegisterNameIndex":
        targets = [target] if isinstance(target, TargetSide) else list(target)
        spans: list[tuple[int, int, int, str]] = []
        for side in targets:
            for name, pt in side.points.items():
                spans.append((side.function_code, pt.addr, _MEASUREMENT_WORDS, name))
        for name, addr in _IDENTITY_REGISTER_ADDRS.items():
            spans.append((3, addr, _IDENTITY_WORDS, name))
        for name, addr in _EXTRA_IDENTITY_ADDRS.items():
            spans.append((3, addr, _IDENTITY_WORDS, name))
        if sigen_identity is not None:
            for name, point in sigen_identity.points.items():
                spans.append(
                    (
                        sigen_identity.function_code,
                        point.addr,
                        point.register_count,
                        name,
                    )
                )
        if sigen_zero_ranges is not None:
            for item in sigen_zero_ranges.ranges:
                spans.append(
                    (
                        sigen_zero_ranges.function_code,
                        item.addr,
                        item.count,
                        item.name,
                    )
                )
        return cls(spans)

    def lookup(self, address: int, count: int, function_code: int = 3) -> list[str]:
        """Names whose register range overlaps the block [address, address+count)."""
        end = address + count
        return [
            name
            for fc, start, words, name in self._spans
            if fc == function_code and start < end and address < start + words
        ]

    def reference_lines(self) -> list[str]:
        """One function-code/address line per known register for a startup banner."""
        return [
            f"  FC{fc:02d} {start:>5} (0x{start:04X}) x{words}  {name}"
            for fc, start, words, name in self._spans
        ]


class LoggingRtuActivity(RtuActivity):
    """RtuActivity that also logs each read request annotated with register names.

    Subclassing RtuActivity keeps the summary()/blocks interface intact, so it can
    still flow through build_context/RecordingSlaveContext with no core changes.
    """

    def __init__(self, index: RegisterNameIndex, logger: logging.Logger = log, **kwargs) -> None:
        super().__init__(**kwargs)
        self._index = index
        self._log = logger

    def record(self, fc: int, address: int, count: int, ts: float) -> None:
        super().record(fc, address, count, ts)
        names = self._index.lookup(address, count, function_code=fc)
        self._log.info(
            "READ FC%02d addr=%d (0x%04X) count=%d -> %s",
            fc,
            address,
            address,
            count,
            ", ".join(names) if names else "(unmapped)",
        )


async def run_rtudebug(
    config: AppConfig, registers: RegisterMap, stop_event: asyncio.Event
) -> None:
    """Run the bridge and log every DTSU register block read by Sigenergy."""
    index = RegisterNameIndex.build(
        [registers.dtsu_target, registers.dtsu_sigen_ext_target],
        registers.dtsu_sigen_identity,
        registers.dtsu_sigen_zero_ranges,
    )
    activity = LoggingRtuActivity(index)
    pipe = build_pipeline(config, registers, stop_event, activity=activity)

    dtsu = config.dtsu
    where = dtsu.rtu.port if dtsu.transport == "rtu" and dtsu.rtu else str(dtsu.tcp)
    log.info(
        "rtudebug: serving DTSU666 as slave %d over %s (%s); logging Sigenergy reads",
        dtsu.slave_id,
        dtsu.transport,
        where,
    )
    log.info("DTSU/Sigen register map (function / addr / words / name):")
    for line in index.reference_lines():
        log.info("%s", line)

    if not await connect_with_retry(
        pipe.client, stop_event,
        config.nd45.reconnect_delay_s, config.nd45.reconnect_delay_max_s,
    ):
        for coro in pipe.coros:
            coro.close()
        pipe.client.close()
        return

    try:
        await asyncio.gather(*pipe.coros)
    finally:
        pipe.client.close()
