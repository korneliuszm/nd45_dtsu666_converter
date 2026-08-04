"""DTSU666 output server (RTU or TCP): datastore build/update + fail-safe supervisor."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.framer.rtu_framer import ModbusRtuFramer
from pymodbus.framer.socket_framer import ModbusSocketFramer
from pymodbus.server import ModbusSerialServer, ModbusTcpServer

from .codec import encode_point
from .config import DtsuConf, StaticIdentitySide, TargetPoint, TargetSide

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
    "ir_at": 0x0006,  # IrAt     - CT ratio, used directly (raw value == ratio)
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
    # Observed on a live meter read by Sigenergy every ~5.4s; always 0.
    slave.setValues(_HOLDING_FC, 0x0046, [0])
    # Fixed non-zero config registers observed on the real TPX-CH meter but of
    # undocumented meaning. 0x0004 falls inside Sigenergy's 0x0003/qty5 config
    # read, so it is served to match the genuine meter exactly; 0x0008 is
    # outside any Sigenergy read but seeded for full-scan fidelity.
    slave.setValues(_HOLDING_FC, 0x0004, [1])
    slave.setValues(_HOLDING_FC, 0x0008, [4])


def write_sigen_identity(
    slave: ModbusSlaveContext, identity: StaticIdentitySide
) -> None:
    """Seed configured Sigen OEM identity values into FC03 holding registers."""
    for point in identity.points.values():
        if point.type == "ascii":
            raw = point.static_value.encode("ascii")
            padded = raw.ljust(point.register_count * 2, b"\0")
            values = [
                (padded[index] << 8) | padded[index + 1]
                for index in range(0, len(padded), 2)
            ]
        else:
            value = point.static_value
            values = [(value >> 16) & 0xFFFF, value & 0xFFFF]
        slave.setValues(identity.function_code, point.addr, values)


def _targets(value: TargetSide | Iterable[TargetSide]) -> list[TargetSide]:
    if isinstance(value, TargetSide):
        return [value]
    return list(value)


_RTU_REQUEST_LEN = 8  # uid, fc, addr(2), count(2), crc(2) -- FC01..FC06 requests
_REQUEST_FUNCTION_CODES = frozenset({1, 2, 3, 4, 5, 6})


def rtu_crc(data: bytes) -> int:
    """Modbus RTU CRC16, little-endian on the wire."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def scan_rtu_requests(buffer: bytes) -> list[tuple[int, int, int, int]]:
    """Every CRC-valid 8-byte request frame in `buffer` as (slave, fc, addr, count).

    Deliberately CRC-checked rather than "first byte is the slave id": this feeds
    a diagnostic that tells the installer which address a master is polling, and
    a wrong answer there sends them chasing the wrong fault.
    """
    found: list[tuple[int, int, int, int]] = []
    for i in range(0, max(0, len(buffer) - _RTU_REQUEST_LEN + 1)):
        frame = buffer[i:i + _RTU_REQUEST_LEN]
        if frame[1] not in _REQUEST_FUNCTION_CODES:
            continue
        if rtu_crc(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
            continue
        slave, fc = frame[0], frame[1]
        addr = int.from_bytes(frame[2:4], "big")
        count = int.from_bytes(frame[4:6], "big")
        found.append((slave, fc, addr, count))
    return found


class WireActivity:
    """Raw traffic on an RTU output port, counted *before* slave-id filtering.

    pymodbus's RTU framer silently drops a frame addressed to a slave the server
    does not hold: ModbusRtuFramer.frameProcessIncomingPacket skips those bytes
    and logs only at DEBUG, so RecordingSlaveContext never sees them. That makes
    the two most common bring-up faults on a fresh RS-485 bus look identical --
    nothing is polling this port at all, versus something is polling it under a
    different slave address. This separates them.
    """

    def __init__(self, max_ids: int = 8) -> None:
        self.bytes_seen = 0
        self.last_ts: float | None = None
        self.frames: Counter[int] = Counter()  # slave id -> CRC-valid requests seen
        self._max_ids = max_ids
        self._tail = b""

    def record(self, data: bytes, ts: float) -> None:
        if not data:
            return
        self.bytes_seen += len(data)
        self.last_ts = ts
        # Keep a short tail so a frame split across two reads is still matched.
        buffer = self._tail + data
        for slave, _fc, _addr, _count in scan_rtu_requests(buffer):
            if slave in self.frames or len(self.frames) < self._max_ids:
                self.frames[slave] += 1
        self._tail = buffer[-(_RTU_REQUEST_LEN - 1):]

    def summary(self, now: float) -> dict:
        return {
            "bytes_seen": self.bytes_seen,
            "last_seen_age": None if self.last_ts is None else now - self.last_ts,
            "slave_ids": self.frames.most_common(),
        }


def tracing_rtu_framer(wire: WireActivity, clock: Callable[[], float] = time.monotonic):
    """A ModbusRtuFramer subclass that feeds `wire` every chunk read from the port.

    pymodbus takes a framer *class* and instantiates it itself, so the tracker is
    closed over here rather than passed in.
    """

    class _TracingRtuFramer(ModbusRtuFramer):
        def processIncomingPacket(self, data, callback, slave, **kwargs):  # noqa: N802
            wire.record(data, clock())
            return super().processIncomingPacket(data, callback, slave, **kwargs)

    return _TracingRtuFramer


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
        self._addr_last_seen: dict[tuple[int, int], float] = {}

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
        # per-register freshness, so the monitor can flag which individual
        # DTSU points Sigenergy is actually polling vs. never touching
        for a in range(address, address + count):
            self._addr_last_seen[(fc, a)] = ts

    def last_seen(self, fc: int, address: int) -> float | None:
        """Timestamp a single register address was last covered by a read, or None."""
        return self._addr_last_seen.get((fc, address))

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
    target: TargetSide | Iterable[TargetSide],
    slave_id: int,
    activity: RtuActivity | None = None,
    dtsu_cfg: DtsuConf | None = None,
    sigen_identity: StaticIdentitySide | None = None,
) -> ModbusServerContext:
    target_list = _targets(target)
    max_addresses = {3: 0, 4: 0}
    for side in target_list:
        max_addresses[side.function_code] = max(
            max_addresses[side.function_code],
            max((point.addr + 1 for point in side.points.values()), default=0),
        )
    if sigen_identity is not None:
        max_addresses[3] = max(
            max_addresses[3],
            max(
                (
                    point.addr + point.register_count - 1
                    for point in sigen_identity.points.values()
                ),
                default=0,
            ),
        )
    holding = ModbusSequentialDataBlock(0, [0] * (max_addresses[3] + 1))
    inputs = ModbusSequentialDataBlock(0, [0] * (max_addresses[4] + 1))
    if activity is not None:
        slave = RecordingSlaveContext(activity, hr=holding, ir=inputs, zero_mode=True)
    else:
        slave = ModbusSlaveContext(hr=holding, ir=inputs, zero_mode=True)
    if dtsu_cfg is not None:
        write_static_registers(slave, slave_id, dtsu_cfg)
    if sigen_identity is not None:
        write_sigen_identity(slave, sigen_identity)
    return ModbusServerContext(slaves={slave_id: slave}, single=False)


def update_datastore(
    context: ModbusServerContext,
    slave_id: int,
    canonical: dict[str, float],
    target: TargetSide | Iterable[TargetSide],
    ct_ratio: float = 1.0,
) -> None:
    """Encode canonical SI values into every target's registers.

    Every point is encoded before the first datastore write, so a validation
    failure preserves the complete previous register image.

    `ct_ratio` is the configured CT ratio (`dtsu.identity.ir_at`): points with
    `divide_by_ct` (the classic DTSU666 map's secondary-side current, power,
    and energy points) are divided by it before scaling, since the ND45
    source and Sigen OEM map (`dtsu_sigen_ext_target`/`_ext_energy`) carry
    primary-side values. See docs/superpowers/specs for the CT-ratio design.
    """
    slave = context[slave_id]
    pending: list[tuple[int, int, list[int]]] = []
    for side in _targets(target):
        for pt in side.points.values():
            si = canonical.get(pt.from_)
            if si is None:
                continue
            regs = encode_target_point(si, pt, side, ct_ratio=ct_ratio)
            pending.append((side.function_code, pt.addr, regs))

    for function_code, address, registers in pending:
        slave.setValues(function_code, address, registers)


def encode_target_point(
    si: float,
    point: TargetPoint,
    target: TargetSide,
    ct_ratio: float = 1.0,
) -> list[int]:
    """Apply target-side conversion and encode one canonical SI value."""
    if point.divide_by_ct:
        if not math.isfinite(ct_ratio) or ct_ratio <= 0:
            raise ValueError("CT ratio must be a positive finite number")
        si = si / ct_ratio
    return encode_point(
        si,
        point.scale,
        point.sign,
        point.offset,
        target.word_order,
        target.byte_order,
        zero_low_word=point.zero_low_word,
    )


def make_serial_server(
    cfg: DtsuConf, context, wire: WireActivity | None = None
) -> ModbusSerialServer:
    return ModbusSerialServer(
        context=context,
        framer=ModbusRtuFramer if wire is None else tracing_rtu_framer(wire),
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


def make_server(
    cfg: DtsuConf, context, wire: WireActivity | None = None
) -> ModbusSerialServer | ModbusTcpServer:
    if cfg.transport == "tcp":
        return make_tcp_server(cfg, context)
    return make_serial_server(cfg, context, wire=wire)


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
    status=None,
    wire: WireActivity | None = None,
) -> None:
    """Start the DTSU output server (RTU or TCP, per cfg.transport) while data is
    fresh; stop it (silence) while stale.

    `status` (duck-typed `metrics.ServerStatus`, optional) records the up/down
    transitions. Server state is otherwise loop-local and unobservable from
    outside, and it cannot be re-derived from the freshness gate: a server whose
    transport never opened still looks 'should serve'."""
    factory = server_factory or (lambda: make_server(cfg, context, wire=wire))
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

    def _mark_down() -> None:
        if status is not None:
            status.stopped()

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
                _mark_down()
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
                _mark_down()
            action = _server_action(
                gate.should_serve(age), server is not None, t, last_start, min_restart_interval
            )
            if action == "start":
                server = factory()
                serve_task = asyncio.create_task(server.serve_forever())
                last_start = t
                if status is not None:
                    status.started(t)
                log.info(
                    "DTSU server started (transport=%s, data fresh, age=%.2fs)",
                    cfg.transport, age,
                )
            elif action == "stop":
                await _shutdown_quietly(server)
                if serve_task:
                    await _cancel_and_await(serve_task)
                server, serve_task = None, None
                _mark_down()
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
        _mark_down()
