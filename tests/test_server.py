import asyncio

import pytest
from pymodbus.server import ModbusSerialServer, ModbusTcpServer

from nd45_dtsu666.canonical import CanonicalStore, HealthGate
from nd45_dtsu666.codec import registers_to_float
from nd45_dtsu666.config import DtsuConf, DtsuRtuConf, DtsuTcpConf, load_registers
from nd45_dtsu666.dtsu_server import (
    RecordingSlaveContext,
    RtuActivity,
    _server_action,
    build_context,
    make_serial_server,
    make_server,
    make_tcp_server,
    supervise_server,
    update_datastore,
)


def _read_point(context, slave_id, pt, target):
    # Inverse of encode_point (raw = si*sign*scale + offset): recover SI from the
    # DTSU register. decode_point cannot be used here because it multiplies by
    # scale (source-side convention), while the target side stores si*scale.
    regs = context[slave_id].getValues(3, pt.addr, count=2)
    raw = registers_to_float(regs, target.word_order, target.byte_order)
    return (raw - pt.offset) / (pt.scale * pt.sign)


def test_update_datastore_encodes_voltage_and_power():
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    update_datastore(context, 1, {"u_l1": 230.0, "p_total": 1500.0}, target)

    u = _read_point(context, 1, target.points["u_l1"], target)
    p = _read_point(context, 1, target.points["p_total"], target)
    assert u == pytest.approx(230.0, rel=1e-5)
    assert p == pytest.approx(1500.0, rel=1e-5)


def test_datastore_raw_scaling_matches_dtsu_spec():
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    update_datastore(context, 1, {"u_l1": 230.0}, target)
    # DTSU voltage is x10 -> raw register float must be 2300.0
    regs = context[1].getValues(3, target.points["u_l1"].addr, count=2)
    assert registers_to_float(regs, target.word_order, target.byte_order) == pytest.approx(2300.0)


def test_missing_canonical_key_is_skipped():
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    update_datastore(context, 1, {}, target)  # no values -> no crash
    regs = context[1].getValues(3, target.points["u_l1"].addr, count=2)
    assert regs == [0, 0]


def test_static_registers_absent_without_dtsu_cfg():
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    assert context[1].getValues(3, 0x0006, count=1) == [0]


def test_static_registers_seeded_from_dtsu_cfg():
    target = load_registers("config/registers.json").dtsu_target
    cfg = DtsuConf(transport="rtu", slave_id=7, rtu=DtsuRtuConf(port="/dev/null", baudrate=9600))
    context = build_context(target, slave_id=7, dtsu_cfg=cfg)
    slave = context[7]
    assert slave.getValues(3, 0x0000, count=1) == [100]  # REV.
    assert slave.getValues(3, 0x0003, count=1) == [0]  # net = 3P4W
    assert slave.getValues(3, 0x0006, count=1) == [10]  # IrAt = ratio 1.0
    assert slave.getValues(3, 0x0007, count=1) == [10]  # UrAt = ratio 1.0
    assert slave.getValues(3, 0x002D, count=1) == [3]  # bAud = 9600
    assert slave.getValues(3, 0x002E, count=1) == [7]  # Addr = slave_id


def test_static_registers_unknown_baudrate_defaults_to_9600_code():
    target = load_registers("config/registers.json").dtsu_target
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/null", baudrate=115200))
    context = build_context(target, slave_id=1, dtsu_cfg=cfg)
    assert context[1].getValues(3, 0x002D, count=1) == [3]


class FakeServer:
    def __init__(self):
        self.running = False
        self.serve_calls = 0
        self.shutdown_calls = 0

    async def serve_forever(self):
        self.running = True
        self.serve_calls += 1
        while self.running:
            await asyncio.sleep(0.01)

    async def shutdown(self):
        self.running = False
        self.shutdown_calls += 1


async def test_supervisor_starts_when_fresh_and_stops_when_stale():
    store = CanonicalStore()
    gate = HealthGate(max_age=3.0)
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/null"))
    fake = FakeServer()
    stop = asyncio.Event()
    clock = {"t": 100.0}

    def now():
        return clock["t"]

    store.update({"p_total": 1.0}, ts=now())  # fresh
    task = asyncio.create_task(
        supervise_server(cfg, context=None, store=store, gate=gate,
                         check_interval=0.02, stop_event=stop,
                         server_factory=lambda: fake, now=now)
    )
    await asyncio.sleep(0.1)
    assert fake.running is True  # serving while fresh

    clock["t"] = 200.0  # data now stale (age 100s > 3s)
    await asyncio.sleep(0.1)
    assert fake.running is False  # stopped -> Sigenergy sees timeout

    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert fake.serve_calls >= 1 and fake.shutdown_calls >= 1


class FailingServer:
    def __init__(self):
        self.serve_calls = 0

    async def serve_forever(self):
        self.serve_calls += 1
        raise RuntimeError("port busy")

    async def shutdown(self):
        pass


async def test_supervisor_retries_when_serve_fails():
    store = CanonicalStore()
    gate = HealthGate(max_age=3.0)
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/null"))
    created = []

    def factory():
        s = FailingServer()
        created.append(s)
        return s

    stop = asyncio.Event()
    store.update({"p_total": 1.0}, ts=100.0)
    task = asyncio.create_task(
        supervise_server(cfg, context=None, store=store, gate=gate,
                         check_interval=0.02, stop_event=stop,
                         server_factory=factory, now=lambda: 100.0)
    )
    await asyncio.sleep(0.15)  # several check intervals while data stays fresh
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert len(created) >= 2  # recreated after failure => it retried


def test_rtu_activity_summary_tally_and_last_seen():
    a = RtuActivity()
    a.record(3, 8192, 64, ts=100.0)
    a.record(3, 8192, 64, ts=100.5)
    a.record(3, 4126, 24, ts=101.0)
    s = a.summary(now=102.0)
    assert s["total"] == 3
    assert s["last_seen_age"] == pytest.approx(1.0)  # 102.0 - 101.0
    blocks = dict(s["blocks"])
    assert blocks[(3, 8192, 64)] == 2
    assert blocks[(3, 4126, 24)] == 1
    assert s["recent"][-1] == (101.0, 3, 4126, 24)


def test_rtu_activity_summary_empty():
    s = RtuActivity().summary(now=5.0)
    assert s["total"] == 0
    assert s["last_seen_age"] is None
    assert s["blocks"] == []


def test_recording_context_records_reads_and_delegates():
    target = load_registers("config/registers.json").dtsu_target
    activity = RtuActivity()
    context = build_context(target, slave_id=1, activity=activity)
    update_datastore(context, 1, {"u_l1": 230.0}, target)
    addr = target.points["u_l1"].addr

    regs = context[1].getValues(3, addr, count=2)

    assert regs != [0, 0]  # real datastore values delegated through
    assert activity.total == 1  # the Sigenergy read was recorded
    s = activity.summary(now=activity.last_ts)
    assert ((3, addr, 2), 1) in s["blocks"]


def test_build_context_activity_optional():
    target = load_registers("config/registers.json").dtsu_target
    plain = build_context(target, slave_id=1)
    assert not isinstance(plain[1], RecordingSlaveContext)
    rec = build_context(target, slave_id=1, activity=RtuActivity())
    assert isinstance(rec[1], RecordingSlaveContext)


def test_server_action_decisions():
    # fresh + idle + never started -> start immediately
    assert _server_action(True, False, now=100.0, last_start=None, min_restart_interval=5.0) == "start"
    # fresh + idle but a restart would be too soon after the last start -> wait (anti-flap)
    assert _server_action(True, False, now=102.0, last_start=100.0, min_restart_interval=5.0) == "wait"
    # fresh + idle and enough time has passed -> start
    assert _server_action(True, False, now=106.0, last_start=100.0, min_restart_interval=5.0) == "start"
    # stale + running -> stop (fail-safe)
    assert _server_action(False, True, now=100.0, last_start=100.0, min_restart_interval=5.0) == "stop"
    # steady states -> no action
    assert _server_action(True, True, now=100.0, last_start=100.0, min_restart_interval=5.0) == "none"
    assert _server_action(False, False, now=100.0, last_start=100.0, min_restart_interval=5.0) == "none"


async def test_supervisor_throttles_restart_within_interval():
    store = CanonicalStore()
    gate = HealthGate(max_age=3.0)
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/null"))
    created = []

    class FailingServer:
        async def serve_forever(self):
            raise RuntimeError("port busy")

        async def shutdown(self):
            pass

    def factory():
        s = FailingServer()
        created.append(s)
        return s

    stop = asyncio.Event()
    store.update({"p_total": 1.0}, ts=100.0)
    task = asyncio.create_task(
        supervise_server(cfg, context=None, store=store, gate=gate,
                         check_interval=0.02, stop_event=stop,
                         server_factory=factory, now=lambda: 100.0,
                         min_restart_interval=5.0)
    )
    await asyncio.sleep(0.15)  # many check intervals, clock frozen at 100.0
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert len(created) == 1  # first server failed; retry throttled inside the interval


async def test_make_tcp_server_uses_tcp_config():
    # async def (not def): pymodbus 3.6.9's ModbusTcpServer.__init__ calls
    # asyncio.get_running_loop() directly, so construction requires a running
    # loop -- pytest-asyncio (asyncio_mode=auto) supplies one for async tests.
    cfg = DtsuConf(transport="tcp", slave_id=1, tcp=DtsuTcpConf(host="127.0.0.1", port=1502))
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    server = make_tcp_server(cfg, context)
    assert isinstance(server, ModbusTcpServer)
    assert server.comm_params.source_address == ("127.0.0.1", 1502)


async def test_make_serial_server_uses_rtu_config():
    # async def: see note in test_make_tcp_server_uses_tcp_config above --
    # ModbusSerialServer construction likewise needs a running event loop.
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/ttyUSB9", baudrate=19200))
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    server = make_serial_server(cfg, context)
    assert isinstance(server, ModbusSerialServer)
    assert server.comm_params.baudrate == 19200


async def test_make_server_dispatches_by_transport():
    # async def: see note above -- both server types need a running event loop.
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    tcp_cfg = DtsuConf(transport="tcp", slave_id=1, tcp=DtsuTcpConf(port=1502))
    rtu_cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/null"))
    assert isinstance(make_server(tcp_cfg, context), ModbusTcpServer)
    assert isinstance(make_server(rtu_cfg, context), ModbusSerialServer)


def test_static_registers_tcp_transport_uses_fixed_baud_code():
    target = load_registers("config/registers.json").dtsu_target
    cfg = DtsuConf(transport="tcp", slave_id=1, tcp=DtsuTcpConf())
    context = build_context(target, slave_id=1, dtsu_cfg=cfg)
    assert context[1].getValues(3, 0x002D, count=1) == [3]  # bAud = fixed 9600 code


class DeadStartServer:
    """Mimics pymodbus 3.6.9 when listen() swallows an OSError (bad serial
    port, busy TCP port): serve_forever() hangs on `await self.serving`
    without raising, and the transport never opens (is_active() stays False).
    """

    def __init__(self):
        self.shutdown_calls = 0
        self._serving = asyncio.Event()

    async def serve_forever(self):
        await self._serving.wait()

    async def shutdown(self):
        self.shutdown_calls += 1
        self._serving.set()

    def is_active(self):
        return False


async def test_supervisor_detects_silent_listen_failure_and_retries():
    store = CanonicalStore()
    gate = HealthGate(max_age=1e9)  # data always fresh; isolate the dead-start logic
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/null"))
    created = []

    def factory():
        s = DeadStartServer()
        created.append(s)
        return s

    stop = asyncio.Event()
    clock = {"t": 100.0}
    store.update({"p_total": 1.0}, ts=100.0)
    task = asyncio.create_task(
        supervise_server(cfg, context=None, store=store, gate=gate,
                         check_interval=0.02, stop_event=stop,
                         server_factory=factory, now=lambda: clock["t"],
                         dead_start_grace=1.0)
    )
    await asyncio.sleep(0.05)  # server started; grace not yet elapsed
    assert len(created) == 1 and created[0].shutdown_calls == 0

    clock["t"] = 102.0  # past the grace period with is_active() still False
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    # the dead start was reaped (socket-less server closed) and retried
    assert created[0].shutdown_calls >= 1
    assert len(created) >= 2


class StopShutdownRaisesServer:
    def __init__(self):
        self.running = False
        self.shutdown_calls = 0

    async def serve_forever(self):
        self.running = True
        while self.running:
            await asyncio.sleep(0.01)

    async def shutdown(self):
        self.shutdown_calls += 1
        self.running = False
        raise OSError("serial device vanished")


async def test_supervisor_survives_shutdown_raising_on_stale_stop(caplog):
    store = CanonicalStore()
    gate = HealthGate(max_age=3.0)
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/null"))
    fake = StopShutdownRaisesServer()
    stop = asyncio.Event()
    clock = {"t": 100.0}

    store.update({"p_total": 1.0}, ts=100.0)  # fresh -> server starts
    task = asyncio.create_task(
        supervise_server(cfg, context=None, store=store, gate=gate,
                         check_interval=0.02, stop_event=stop,
                         server_factory=lambda: fake, now=lambda: clock["t"])
    )
    await asyncio.sleep(0.05)
    assert fake.running is True

    clock["t"] = 200.0  # stale -> "stop" path; shutdown() raises OSError
    with caplog.at_level("WARNING", logger="nd45_dtsu666.dtsu_server"):
        await asyncio.sleep(0.1)

    # an unplugged serial adapter during fail-safe must not crash the bridge
    assert fake.shutdown_calls == 1
    assert not task.done()  # supervisor survived the OSError
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)  # exits cleanly, no exception


class CrashAndShutdownRaisesServer:
    async def serve_forever(self):
        raise RuntimeError("boom")

    async def shutdown(self):
        raise OSError("also broken")


async def test_supervisor_survives_shutdown_raising_after_crash():
    store = CanonicalStore()
    gate = HealthGate(max_age=3.0)
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/null"))
    created = []

    def factory():
        s = CrashAndShutdownRaisesServer()
        created.append(s)
        return s

    stop = asyncio.Event()
    store.update({"p_total": 1.0}, ts=100.0)
    task = asyncio.create_task(
        supervise_server(cfg, context=None, store=store, gate=gate,
                         check_interval=0.02, stop_event=stop,
                         server_factory=factory, now=lambda: 100.0)
    )
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert len(created) >= 2  # kept retrying despite shutdown() also failing


def test_rtu_activity_blocks_tracking_is_bounded():
    a = RtuActivity(max_blocks=4)
    for addr in range(100):  # a scanning client hitting distinct addresses
        a.record(3, addr, 2, ts=float(addr))
    s = a.summary(now=200.0)
    assert s["total"] == 100  # total still counts everything
    assert len(s["blocks"]) <= 4  # per-block tally must not grow unbounded

    a.record(3, 0, 2, ts=150.0)  # an already-tracked block keeps counting
    blocks = dict(a.summary(now=200.0)["blocks"])
    assert blocks[(3, 0, 2)] == 2


class TrackedFailingServer:
    def __init__(self):
        self.serve_calls = 0
        self.shutdown_calls = 0

    async def serve_forever(self):
        self.serve_calls += 1
        raise RuntimeError("port busy")

    async def shutdown(self):
        self.shutdown_calls += 1


async def test_supervisor_closes_server_after_unexpected_task_failure():
    store = CanonicalStore()
    gate = HealthGate(max_age=3.0)
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/null"))
    created = []

    def factory():
        s = TrackedFailingServer()
        created.append(s)
        return s

    stop = asyncio.Event()
    store.update({"p_total": 1.0}, ts=100.0)
    task = asyncio.create_task(
        supervise_server(cfg, context=None, store=store, gate=gate,
                         check_interval=0.02, stop_event=stop,
                         server_factory=factory, now=lambda: 100.0)
    )
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    # the crashed server must be closed (freeing its socket/fd) before being
    # discarded, not just dropped on the floor -- otherwise repeated failures
    # over a long-running deployment leak one fd each time.
    assert created[0].shutdown_calls >= 1


class CancelRaisesServer:
    def __init__(self):
        self.running = False
        self.shutdown_calls = 0

    async def serve_forever(self):
        self.running = True
        try:
            while self.running:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            raise RuntimeError("cleanup failed") from None

    async def shutdown(self):
        self.running = False
        self.shutdown_calls += 1


async def test_supervisor_logs_exception_from_cancelled_server_task(caplog):
    store = CanonicalStore()
    gate = HealthGate(max_age=3.0)
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/null"))
    fake = CancelRaisesServer()
    stop = asyncio.Event()
    clock = {"t": 100.0}

    def now():
        return clock["t"]

    store.update({"p_total": 1.0}, ts=now())  # fresh -> server starts
    task = asyncio.create_task(
        supervise_server(cfg, context=None, store=store, gate=gate,
                         check_interval=0.02, stop_event=stop,
                         server_factory=lambda: fake, now=now)
    )
    await asyncio.sleep(0.05)
    assert fake.running is True

    clock["t"] = 200.0  # data now stale -> triggers the "stop" (cancel) path
    with caplog.at_level("WARNING"):
        await asyncio.sleep(0.1)

    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    # cancelling the server task raised RuntimeError("cleanup failed") instead
    # of a clean CancelledError. The app must await and log that itself
    # (attributed to our own logger) rather than leaving it to asyncio's
    # generic, unattributed "Task exception was never retrieved" handler.
    app_records = [r for r in caplog.records if r.name == "nd45_dtsu666.dtsu_server"]
    assert any("cleanup failed" in r.getMessage() for r in app_records)
    assert not any(
        r.name == "asyncio" and "never retrieved" in r.getMessage() for r in caplog.records
    )
