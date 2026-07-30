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

from . import metrics
from .app import build_pipeline, connect_with_retry
from .config import (
    AppConfig,
    RegisterMap,
    StaticIdentitySide,
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
    config: AppConfig, registers: RegisterMap, stop_event: asyncio.Event,
    bridge_name: str | None = None,
) -> None:
    """Run every bridge, logging the DTSU register blocks one of them serves.

    The logging recorder attaches to a single bridge (`bridge_name`, default the
    first): the point of this mode is a readable trace of one Sigenergy master's
    reads, and interleaving two buses would defeat that. The other bridges still
    run normally so the process behaves as it does in production.
    """
    index = RegisterNameIndex.build(
        [side for _name, side in registers.targets],
        registers.dtsu_sigen_identity,
    )
    activity = LoggingRtuActivity(index)
    pipe = build_pipeline(config, registers, stop_event, activity=activity, mode="rtudebug")
    watched = pipe.primary if bridge_name is None else pipe.bridge(bridge_name)
    metrics_task = metrics.start(config, pipe.metrics, stop_event)

    dtsu = watched.spec.dtsu
    where = dtsu.rtu.port if dtsu.transport == "rtu" and dtsu.rtu else str(dtsu.tcp)
    log.info(
        "rtudebug: tracing bridge %r -- serving DTSU666 as slave %d over %s (%s)",
        watched.name, dtsu.slave_id, dtsu.transport, where,
    )
    others = [b.name for b in pipe.bridges if b.name != watched.name]
    if others:
        log.info("also running (untraced): %s", ", ".join(others))
    log.info("DTSU/Sigen register map (function / addr / words / name):")
    for line in index.reference_lines():
        log.info("%s", line)

    connects = [
        asyncio.create_task(
            connect_with_retry(
                bridge.client, stop_event,
                bridge.spec.source.reconnect_delay_s,
                bridge.spec.source.reconnect_delay_max_s,
                heartbeat=bridge.heartbeat,
            )
        )
        for bridge in pipe.bridges
    ]
    try:
        await asyncio.gather(*pipe.coros)
    finally:
        for task in connects:
            task.cancel()
        for task in connects:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - shutdown must not mask the exit reason
                pass
        await metrics.stop(metrics_task)
        for bridge in pipe.bridges:
            close = getattr(bridge.client, "close", None)
            if close is not None:
                close()
