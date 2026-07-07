"""DTSU666 output server (RTU or TCP): datastore build/update + fail-safe supervisor."""

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
from pymodbus.framer.socket_framer import ModbusSocketFramer
from pymodbus.server import ModbusSerialServer, ModbusTcpServer

from .codec import encode_point
from .config import DtsuConf, TargetSide

log = logging.getLogger(__name__)

_HOLDING_FC = 3

# DTSU666 identity/config registers with no ND45 measurement equivalent (signed
# int16, 1 word each -- NOT the float32 2-word format used for measurement data).
# Addresses are fixed by the DTSU666 protocol; values come from DtsuConf.identity
# (config/config.json `dtsu.identity`) so a specific real meter's identity/CT/PT
# ratios can be entered by hand without touching code.
_IDENTITY_REGISTER_ADDRS: dict[str, int] = {
    "rev": 0x0000,  # REV.     - firmware version (arbitrary; not validated by masters)
    "ucode": 0x0001,  # UCode    - programming code
    "clr_e": 0x0002,  # CLr.E    - energy clear command
    "net": 0x0003,  # net      - network mode: 0 = 3P4W, 1 = 3P3W
    "ir_at": 0x0006,  # IrAt     - current transformer ratio, x0.1 -> 10 = ratio 1.0
    "ur_at": 0x0007,  # UrAt     - voltage transformer ratio, x0.1 -> 10 = ratio 1.0
    "disp": 0x000A,  # Disp     - display rotation time
    "b_lcd": 0x000B,  # B.LCD    - backlight time
    "endian": 0x000C,  # Endian   - reserved
    "protocol": 0x002C,  # Protocol - protocol/parity selection
}

# bAud (0x002DH): meter-reported serial speed code, per DTSU666 manual.
_BAUD_CODES: dict[int, int] = {1200: 0, 2400: 1, 4800: 2, 9600: 3}


def write_static_registers(slave: ModbusSlaveContext, slave_id: int, dtsu_cfg: DtsuConf) -> None:
    """Seed the DTSU666 identity/config registers (0x0000-0x002E block) once."""
    for field, addr in _IDENTITY_REGISTER_ADDRS.items():
        value = getattr(dtsu_cfg.identity, field)
        slave.setValues(_HOLDING_FC, addr, [value])
    # bAud reflects a physical baudrate only in RTU mode; TCP has none, so it
    # reports a fixed 9600 code -- the register stays in the map either way
    # since the DTSU666 format must not change.
    baudrate = dtsu_cfg.rtu.baudrate if dtsu_cfg.transport == "rtu" else 9600
    baud_code = _BAUD_CODES.get(baudrate)
    if baud_code is None:
        log.warning("No bAud code for baudrate=%d; defaulting to 9600 code", baudrate)
        baud_code = _BAUD_CODES[9600]
    slave.setValues(_HOLDING_FC, 0x002D, [baud_code])  # bAud
    slave.setValues(_HOLDING_FC, 0x002E, [slave_id])  # Addr


class RtuActivity:
    """Records Modbus read requests received from the storage (Sigenergy)."""

    def __init__(
        self, recent_maxlen: int = 8, rate_window: float = 5.0, max_blocks: int = 64
    ) -> None:
        self.total = 0
        self.last_ts: float | None = None
        self._blocks: Counter[tuple[int, int, int]] = Counter()
        self._max_blocks = max_blocks
        self._recent: deque[tuple[float, int, int, int]] = deque(maxlen=recent_maxlen)
        self._times: deque[float] = deque()
        self._rate_window = rate_window

    def record(self, fc: int, address: int, count: int, ts: float) -> None:
        self.total += 1
        self.last_ts = ts
        # cap DISTINCT tracked blocks so a scanning/misbehaving client can't
        # grow this Counter unboundedly over a long-running monitor session;
        # already-tracked blocks keep counting
        key = (fc, address, count)
        if key in self._blocks or len(self._blocks) < self._max_blocks:
            self._blocks[key] += 1
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
    """Slave context that logs every getValues (a read request) into an RtuActivity."""

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
    target: TargetSide,
    slave_id: int,
    activity: RtuActivity | None = None,
    dtsu_cfg: DtsuConf | None = None,
) -> ModbusServerContext:
    max_addr = max(pt.addr for pt in target.points.values())
    block = ModbusSequentialDataBlock(0, [0] * (max_addr + 2))
    if activity is not None:
        slave = RecordingSlaveContext(activity, hr=block, zero_mode=True)
    else:
        slave = ModbusSlaveContext(hr=block, zero_mode=True)
    if dtsu_cfg is not None:
        write_static_registers(slave, slave_id, dtsu_cfg)
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


def make_serial_server(cfg: DtsuConf, context) -> ModbusSerialServer:
    return ModbusSerialServer(
        context=context,
        framer=ModbusRtuFramer,
        port=cfg.rtu.port,
        baudrate=cfg.rtu.baudrate,
        parity=cfg.rtu.parity,
        stopbits=cfg.rtu.stopbits,
        bytesize=8,
    )


def make_tcp_server(cfg: DtsuConf, context) -> ModbusTcpServer:
    return ModbusTcpServer(
        context=context,
        framer=ModbusSocketFramer,
        address=(cfg.tcp.host, cfg.tcp.port),
    )


def make_server(cfg: DtsuConf, context) -> ModbusSerialServer | ModbusTcpServer:
    if cfg.transport == "tcp":
        return make_tcp_server(cfg, context)
    return make_serial_server(cfg, context)


def _server_action(
    fresh: bool, running: bool, now: float, last_start: float | None, min_restart_interval: float
) -> str:
    """Decide the DTSU output server transition. Restarts are throttled by
    `min_restart_interval` to avoid flapping the transport (serial port or TCP
    listener) when ND45 data oscillates around the freshness threshold."""
    if fresh and not running:
        if last_start is None or now - last_start >= min_restart_interval:
            return "start"
        return "wait"
    if not fresh and running:
        return "stop"
    return "none"


def _listen_never_opened(server) -> bool:
    """True when the server's transport never opened (pymodbus 3.6.9's
    listen() swallows OSError -- bad serial port, busy TCP port -- and
    serve_forever() then hangs without raising, so the task never looks
    failed). Duck-typed: fakes without is_active() are never flagged."""
    is_active = getattr(server, "is_active", None)
    return is_active is not None and not is_active()


async def supervise_server(
    cfg,
    context,
    store,
    gate,
    check_interval: float,
    stop_event: asyncio.Event,
    server_factory: Callable | None = None,
    now: Callable[[], float] | None = None,
    min_restart_interval: float = 0.0,
    dead_start_grace: float = 5.0,
) -> None:
    """Start the DTSU output server (RTU or TCP, per cfg.transport) while data is
    fresh; stop it (silence) while stale."""
    factory = server_factory or (lambda: make_server(cfg, context))
    clock = now or time.monotonic
    server = None
    serve_task: asyncio.Task | None = None
    last_start: float | None = None

    async def _cancel_and_await(task: asyncio.Task) -> None:
        """Cancel a server task and retrieve its outcome ourselves, so a
        non-CancelledError raised during shutdown is logged through our own
        logger instead of asyncio's generic "exception was never retrieved"
        handler."""
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - best-effort shutdown logging
            log.warning("DTSU server task raised while shutting down: %r", exc)

    async def _shutdown_quietly(srv) -> None:
        """Best-effort close. shutdown() can itself raise (e.g. pyserial
        OSError after the RS-485 adapter is unplugged); that must degrade
        to a log line, never crash the supervisor -- the fail-safe would
        die with it."""
        try:
            await srv.shutdown()
        except Exception as exc:  # noqa: BLE001 - cleanup must not kill the fail-safe
            log.warning("Error closing DTSU server (best-effort): %r", exc)

    try:
        while not stop_event.is_set():
            t = clock()
            age = store.age(t)
            if server is not None and serve_task is not None and serve_task.done():
                exc = serve_task.exception()
                if exc is not None:
                    log.error("DTSU server task failed: %r; will retry", exc)
                else:
                    log.warning("DTSU server task ended unexpectedly; will retry")
                # Best-effort close: the task ended on its own, so the
                # transport may still hold an open socket -- discarding the
                # reference without closing it would leak a fd on every such
                # failure over a long-running deployment.
                await _shutdown_quietly(server)
                server, serve_task = None, None
            elif (
                server is not None
                and serve_task is not None
                and _listen_never_opened(server)
                and last_start is not None
                and t - last_start >= dead_start_grace
            ):
                log.error(
                    "DTSU server never opened its transport (port missing/busy?); will retry"
                )
                await _shutdown_quietly(server)
                await _cancel_and_await(serve_task)
                server, serve_task = None, None
            action = _server_action(
                gate.should_serve(age), server is not None, t, last_start, min_restart_interval
            )
            if action == "start":
                server = factory()
                serve_task = asyncio.create_task(server.serve_forever())
                last_start = t
                log.info(
                    "DTSU server started (transport=%s, data fresh, age=%.2fs)",
                    cfg.transport, age,
                )
            elif action == "stop":
                await _shutdown_quietly(server)
                if serve_task:
                    await _cancel_and_await(serve_task)
                server, serve_task = None, None
                log.warning(
                    "DTSU server stopped (transport=%s, data stale, age=%.2fs) -> fail-safe",
                    cfg.transport, age,
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        if server is not None:
            await _shutdown_quietly(server)
            if serve_task:
                await _cancel_and_await(serve_task)
