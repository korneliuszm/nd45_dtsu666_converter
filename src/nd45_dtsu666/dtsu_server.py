"""DTSU666 RTU server: datastore build/update + fail-safe supervisor."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque
from collections.abc import Callable

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.framer.rtu_framer import ModbusRtuFramer
from pymodbus.server import ModbusSerialServer

from .codec import encode_point
from .config import TargetSide

log = logging.getLogger(__name__)

_HOLDING_FC = 3


class RtuActivity:
    """Records Modbus RTU read requests received from the storage (Sigenergy)."""

    def __init__(self, recent_maxlen: int = 8, rate_window: float = 5.0) -> None:
        self.total = 0
        self.last_ts: float | None = None
        self._blocks: Counter[tuple[int, int, int]] = Counter()
        self._recent: deque[tuple[float, int, int, int]] = deque(maxlen=recent_maxlen)
        self._times: deque[float] = deque()
        self._rate_window = rate_window

    def record(self, fc: int, address: int, count: int, ts: float) -> None:
        self.total += 1
        self.last_ts = ts
        self._blocks[(fc, address, count)] += 1
        self._recent.append((ts, fc, address, count))
        self._times.append(ts)
        while self._times and ts - self._times[0] > self._rate_window:
            self._times.popleft()

    def summary(self, now: float) -> dict:
        recent_times = [t for t in self._times if now - t <= self._rate_window]
        return {
            "total": self.total,
            "last_seen_age": None if self.last_ts is None else now - self.last_ts,
            "rate": len(recent_times) / self._rate_window,
            "blocks": self._blocks.most_common(),
            "recent": list(self._recent),
        }


class RecordingSlaveContext(ModbusSlaveContext):
    """Slave context that logs every getValues (an RTU read) into an RtuActivity."""

    def __init__(
        self, activity: RtuActivity, *args, clock: Callable[[], float] = time.monotonic, **kwargs
    ) -> None:
        super().__init__(*args, **kwargs)
        self._activity = activity
        self._clock = clock

    def getValues(self, fc_as_hex, address, count=1):
        self._activity.record(fc_as_hex, address, count, self._clock())
        return super().getValues(fc_as_hex, address, count)


def build_context(
    target: TargetSide, slave_id: int, activity: RtuActivity | None = None
) -> ModbusServerContext:
    max_addr = max(pt.addr for pt in target.points.values())
    block = ModbusSequentialDataBlock(0, [0] * (max_addr + 2))
    if activity is not None:
        slave = RecordingSlaveContext(activity, hr=block, zero_mode=True)
    else:
        slave = ModbusSlaveContext(hr=block, zero_mode=True)
    return ModbusServerContext(slaves={slave_id: slave}, single=False)


def update_datastore(
    context: ModbusServerContext, slave_id: int, canonical: dict[str, float], target: TargetSide
) -> None:
    slave = context[slave_id]
    wo, bo = target.word_order, target.byte_order
    for pt in target.points.values():
        si = canonical.get(pt.from_)
        if si is None:
            continue
        regs = encode_point(si, pt.scale, pt.sign, pt.offset, wo, bo)
        slave.setValues(_HOLDING_FC, pt.addr, regs)


def make_serial_server(cfg, context) -> ModbusSerialServer:
    return ModbusSerialServer(
        context=context,
        framer=ModbusRtuFramer,
        port=cfg.port,
        baudrate=cfg.baudrate,
        parity=cfg.parity,
        stopbits=cfg.stopbits,
        bytesize=8,
    )


async def supervise_server(
    cfg,
    context,
    store,
    gate,
    check_interval: float,
    stop_event: asyncio.Event,
    server_factory: Callable | None = None,
    now: Callable[[], float] | None = None,
) -> None:
    """Start the RTU server while data is fresh; stop it (silence) while stale."""
    factory = server_factory or (lambda: make_serial_server(cfg, context))
    clock = now or time.monotonic
    server = None
    serve_task: asyncio.Task | None = None

    try:
        while not stop_event.is_set():
            age = store.age(clock())
            if server is not None and serve_task is not None and serve_task.done():
                exc = serve_task.exception()
                if exc is not None:
                    log.error("RTU server task failed: %r; will retry", exc)
                else:
                    log.warning("RTU server task ended unexpectedly; will retry")
                server, serve_task = None, None
            if gate.should_serve(age) and server is None:
                server = factory()
                serve_task = asyncio.create_task(server.serve_forever())
                log.info("RTU server started (data fresh, age=%.2fs)", age)
            elif not gate.should_serve(age) and server is not None:
                await server.shutdown()
                if serve_task:
                    serve_task.cancel()
                server, serve_task = None, None
                log.warning("RTU server stopped (data stale, age=%.2fs) -> fail-safe", age)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        if server is not None:
            await server.shutdown()
            if serve_task:
                serve_task.cancel()
