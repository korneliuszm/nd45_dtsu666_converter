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

from .app import build_pipeline, connect_with_retry
from .config import AppConfig, RegisterMap, TargetSide
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

    def __init__(self, spans: list[tuple[int, int, str]]) -> None:
        # spans: (start_addr, word_len, name), kept sorted by start address.
        self._spans = sorted(spans, key=lambda s: s[0])

    @classmethod
    def build(cls, target: TargetSide) -> "RegisterNameIndex":
        spans: list[tuple[int, int, str]] = []
        for name, pt in target.points.items():
            spans.append((pt.addr, _MEASUREMENT_WORDS, name))
        for name, addr in _IDENTITY_REGISTER_ADDRS.items():
            spans.append((addr, _IDENTITY_WORDS, name))
        for name, addr in _EXTRA_IDENTITY_ADDRS.items():
            spans.append((addr, _IDENTITY_WORDS, name))
        return cls(spans)

    def lookup(self, address: int, count: int) -> list[str]:
        """Names whose register range overlaps the block [address, address+count)."""
        end = address + count
        return [
            name
            for start, words, name in self._spans
            if start < end and address < start + words
        ]

    def reference_lines(self) -> list[str]:
        """One `addr (0xHHHH) name` line per known register, for a startup banner."""
        return [
            f"  {start:>5} (0x{start:04X}) x{words}  {name}"
            for start, words, name in self._spans
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
        names = self._index.lookup(address, count)
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
    index = RegisterNameIndex.build(registers.dtsu_target)
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
    log.info("DTSU register map (addr / words / name):")
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
