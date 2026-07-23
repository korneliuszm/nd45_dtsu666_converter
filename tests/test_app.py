import asyncio
import socket
import time

import pytest

from nd45_dtsu666.app import FaultReporter, build_on_update, build_pipeline, connect_with_retry, run_app
from nd45_dtsu666.canonical import CanonicalStore
from nd45_dtsu666.codec import registers_to_float
from nd45_dtsu666.config import load_config, load_registers
from nd45_dtsu666.dtsu_server import RecordingSlaveContext, RtuActivity, build_context
from nd45_dtsu666.watchdog import Heartbeat


def test_on_update_writes_store_and_datastore():
    target = load_registers("config/registers.json").dtsu_target
    store = CanonicalStore()
    context = build_context(target, slave_id=1)
    on_update = build_on_update(store, context, 1, target)

    on_update({"u_l1": 230.0}, ts=5.0)

    values, ts = store.snapshot()
    assert values["u_l1"] == 230.0 and ts == 5.0
    regs = context[1].getValues(3, target.points["u_l1"].addr, count=2)
    assert registers_to_float(regs, target.word_order, target.byte_order) == 2300.0


def test_on_update_leaves_store_stale_when_datastore_write_fails():
    import math

    target = load_registers("config/registers.json").dtsu_target
    store = CanonicalStore()

    class _BoomContext:
        def __getitem__(self, slave_id):
            class _Slave:
                def setValues(self, *args, **kwargs):
                    raise RuntimeError("datastore write failed")

            return _Slave()

    on_update = build_on_update(store, _BoomContext(), 1, target)

    # a failed datastore write must propagate (poller logs it as a poll fault)
    # and must NOT have stamped the store fresh -- otherwise the fail-safe would
    # keep serving a half-written datastore as if it were valid.
    with pytest.raises(RuntimeError):
        on_update({"u_l1": 230.0}, ts=5.0)
    values, ts = store.snapshot()
    assert values == {}
    assert math.isnan(ts)  # never stamped -> age() stays infinite -> fail-safe


def test_build_pipeline_wires_components_and_threads_activity(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    stop = asyncio.Event()
    activity = RtuActivity()

    pipe = build_pipeline(config, registers, stop, activity=activity, client=object())

    assert pipe.store is not None
    assert len(pipe.coros) == 2  # poller + supervisor
    # activity was threaded into a recording datastore context
    slave = pipe.context[config.dtsu.slave_id]
    assert isinstance(slave, RecordingSlaveContext)
    assert slave.getValues(3, 0xF114, count=2) == [0x0000, 0x1500]
    sigen_u_l1 = registers.dtsu_sigen_ext_target.points["u_l1"]
    assert slave.getValues(4, sigen_u_l1.addr, count=2) == [0, 0]
    assert slave.getValues(4, 0x180A, count=22) == [0] * 22
    assert slave.getValues(4, 0x1828, count=4) == [0] * 4

    for coro in pipe.coros:  # never awaited in this test; close to keep output pristine
        coro.close()


def test_build_pipeline_default_context_not_recording(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    pipe = build_pipeline(config, registers, asyncio.Event(), client=object())
    assert not isinstance(pipe.context[config.dtsu.slave_id], RecordingSlaveContext)
    for coro in pipe.coros:
        coro.close()


async def test_run_app_pings_watchdog_during_initial_connect_retry(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(sock_path)
    server.setblocking(False)
    monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
    monkeypatch.setenv("WATCHDOG_USEC", "200000")  # 0.2s -> pings every 0.1s

    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    stop = asyncio.Event()

    class _NeverConnectingClient:
        def close(self):
            pass

        async def connect(self):
            return False  # ND45 unreachable -- keeps connect_with_retry looping

    task = asyncio.create_task(run_app(config, registers, stop, client=_NeverConnectingClient()))
    await asyncio.sleep(0.15)  # still inside connect_with_retry's backoff wait
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    # a WATCHDOG=1 ping must have arrived WHILE still stuck in the initial
    # connect retry -- proving the watchdog task runs concurrently with
    # connect_with_retry, not only after it succeeds. Drain the queue rather
    # than reading a single datagram: notify_ready() unconditionally sends
    # READY=1 first (correct ordering -- readiness precedes pings), so the
    # ping we care about is not necessarily the first datagram queued.
    pings = []
    while True:
        try:
            data, _ = server.recvfrom(1024)
            pings.append(data)
        except BlockingIOError:
            break
    assert b"WATCHDOG=1" in pings
    server.close()


class _FlakyClient:
    def __init__(self, fail_times):
        self.calls = 0
        self._fail_times = fail_times

    async def connect(self):
        self.calls += 1
        return self.calls > self._fail_times


async def test_connect_with_retry_succeeds_after_failures():
    client = _FlakyClient(fail_times=2)
    ok = await connect_with_retry(client, asyncio.Event(), delay=0.001, max_delay=0.01)
    assert ok is True
    assert client.calls == 3  # failed twice, connected on the third attempt


async def test_connect_with_retry_touches_heartbeat_each_attempt():
    client = _FlakyClient(fail_times=2)
    hb = Heartbeat()
    ok = await connect_with_retry(
        client, asyncio.Event(), delay=0.001, max_delay=0.01, heartbeat=hb
    )
    assert ok is True
    assert hb.age(time.monotonic()) < 1.0  # touched on the connect loop's most recent attempt


async def test_build_pipeline_poller_touches_heartbeat_on_success(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")

    class _FakeClient:
        async def read_holding_registers(self, address, count, slave=0):
            class _Resp:
                registers = [0] * count

                def isError(self):
                    return False

            return _Resp()

    stop = asyncio.Event()
    pipe = build_pipeline(config, registers, stop, client=_FakeClient())
    poller_task = asyncio.create_task(pipe.coros[0])
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(poller_task, timeout=1.0)
    for coro in pipe.coros[1:]:
        coro.close()
    assert pipe.heartbeat.age(time.monotonic()) < 1.0


async def test_connect_with_retry_returns_false_when_stopped():
    client = _FlakyClient(fail_times=999)
    stop = asyncio.Event()
    stop.set()  # asked to stop before ever connecting
    ok = await connect_with_retry(client, stop, delay=0.001, max_delay=0.01)
    assert ok is False
    assert client.calls == 0  # never even attempted


async def test_connect_with_retry_stops_between_attempts():
    client = _FlakyClient(fail_times=999)  # never connects
    stop = asyncio.Event()

    async def _stopper():
        await asyncio.sleep(0.01)
        stop.set()

    ok, _ = await asyncio.gather(
        connect_with_retry(client, stop, delay=0.005, max_delay=0.01), _stopper()
    )
    assert ok is False  # gives up cleanly once stop is set, never hangs


class _FakeLog:
    def __init__(self):
        self.warnings = []
        self.infos = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args):
        self.infos.append(msg % args if args else msg)


def test_fault_reporter_logs_first_failure_then_mutes():
    log = _FakeLog()
    r = FaultReporter(logger=log, summary_interval=60.0, clock=lambda: 0.0)
    for _ in range(5):
        r.failure(Exception("boom"))
    assert len(log.warnings) == 1  # only the first of a burst is logged


def test_fault_reporter_periodic_summary():
    log = _FakeLog()
    t = {"v": 0.0}
    r = FaultReporter(logger=log, summary_interval=60.0, clock=lambda: t["v"])
    r.failure(Exception("boom"))  # t=0 -> first warning
    t["v"] = 30.0
    r.failure(Exception("boom"))  # muted (<60s)
    assert len(log.warnings) == 1
    t["v"] = 61.0
    r.failure(Exception("boom"))  # summary warning
    assert len(log.warnings) == 2


def test_fault_reporter_logs_recovery_and_resets():
    log = _FakeLog()
    r = FaultReporter(logger=log, clock=lambda: 0.0)
    r.failure(Exception("boom"))
    r.failure(Exception("boom"))
    r.success()
    assert len(log.infos) == 1  # recovery logged once
    r.failure(Exception("boom"))
    assert len(log.warnings) == 2  # state reset -> new fault logs again


def test_fault_reporter_success_without_failure_is_silent():
    log = _FakeLog()
    FaultReporter(logger=log, clock=lambda: 0.0).success()
    assert log.warnings == [] and log.infos == []
