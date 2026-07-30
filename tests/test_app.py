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


def test_build_pipeline_records_activity_for_metrics_without_explicit_activity(monkeypatch):
    """`run` passes no activity=, but the metrics endpoint still needs read stats."""
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    assert config.prometheus.enabled
    pipe = build_pipeline(config, registers, asyncio.Event(), client=object())
    assert isinstance(pipe.context[config.dtsu.slave_id], RecordingSlaveContext)
    assert pipe.metrics is not None
    for coro in pipe.coros:
        coro.close()


def test_build_pipeline_default_context_not_recording_without_prometheus(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = load_config("config/config.json")
    config.prometheus.enabled = False
    registers = load_registers("config/registers.json")
    pipe = build_pipeline(config, registers, asyncio.Event(), client=object())
    assert not isinstance(pipe.context[config.dtsu.slave_id], RecordingSlaveContext)
    assert pipe.metrics is None
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


# --- Huawei SmartLogger as a second source ----------------------------------


class _HuaweiFakeClient:
    """Duck-typed stand-in: a real AsyncModbusTcpClient needs a running loop."""

    def __init__(self):
        self.requests = []

    async def connect(self):
        return True

    async def read_holding_registers(self, address, count, slave=0):
        self.requests.append((address, count, slave))

        class _Resp:
            registers = [0] * count

            def isError(self):
                return False

        return _Resp()

    def close(self):
        pass


def _huawei_config(**overrides):
    config = load_config("config/config.json")
    return config.model_copy(
        update={
            "huawei": config.huawei.model_copy(
                update={"enabled": True, "host": "10.0.0.5", **overrides}
            )
        }
    )


def test_build_pipeline_without_huawei_builds_exactly_two_coros(monkeypatch):
    """Regression guard: with the source disabled, nothing about the bridge changes."""
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = load_config("config/config.json")
    assert config.huawei.enabled is False
    pipe = build_pipeline(
        config, load_registers("config/registers.json"), asyncio.Event(), client=object()
    )

    assert len(pipe.coros) == 2  # poller + supervisor, as before
    assert pipe.huawei_store is None
    assert pipe.huawei_client is None
    assert pipe.merged_store is None
    for coro in pipe.coros:
        coro.close()


def test_build_pipeline_with_huawei_adds_a_third_coro(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    pipe = build_pipeline(
        _huawei_config(), load_registers("config/registers.json"),
        asyncio.Event(), client=object(), huawei_client=_HuaweiFakeClient(),
    )

    assert len(pipe.coros) == 3  # ND45 poller + SmartLogger poller + supervisor
    assert pipe.huawei_store is not None
    assert pipe.huawei_client is not None
    assert pipe.merged_store is not None
    # the exporter reads the union, so PV points show up as registers
    assert pipe.metrics.store is pipe.merged_store
    assert pipe.metrics.huawei_store is pipe.huawei_store
    for coro in pipe.coros:
        coro.close()
    pipe.huawei_client.close()


def test_build_pipeline_freshness_still_tracks_the_nd45_alone(monkeypatch):
    """pipe.store stays the primary store, so supervise_server's gate is unchanged."""
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    pipe = build_pipeline(
        _huawei_config(), load_registers("config/registers.json"),
        asyncio.Event(), client=object(), huawei_client=_HuaweiFakeClient(),
    )

    assert isinstance(pipe.store, CanonicalStore)
    pipe.huawei_store.update({"pv_p_total": 1.0}, ts=100.0)
    # a fresh SmartLogger sample must not make the never-polled ND45 look alive
    assert pipe.store.age(now=100.0) == float("inf")
    assert pipe.merged_store.age(now=100.0) == float("inf")
    for coro in pipe.coros:
        coro.close()
    pipe.huawei_client.close()


def test_build_pipeline_rejects_huawei_without_a_register_section(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    registers = load_registers("config/registers.json")
    stripped = registers.model_copy(update={"huawei_plant_source": None})
    with pytest.raises(ValueError, match="no huawei_plant_source section"):
        build_pipeline(
            _huawei_config(), stripped, asyncio.Event(),
            client=object(), huawei_client=_HuaweiFakeClient(),
        )


def test_on_update_merges_a_secondary_store_beneath_its_own_values():
    """Every poll writes the union, so one poller never blanks the other's points."""
    target = load_registers("config/registers.json").dtsu_target
    primary, secondary = CanonicalStore(), CanonicalStore()
    context = build_context(target, slave_id=1)
    secondary.update({"u_l2": 111.0}, ts=1.0)

    on_update = build_on_update(primary, context, 1, target, beneath=(secondary,))
    on_update({"u_l1": 230.0}, ts=5.0)

    # our own value landed...
    u_l1 = context[1].getValues(3, target.points["u_l1"].addr, count=2)
    assert registers_to_float(u_l1, "big", "big") == 2300.0
    # ...and so did the secondary's, without being in our own store
    u_l2 = context[1].getValues(3, target.points["u_l2"].addr, count=2)
    assert registers_to_float(u_l2, "big", "big") == 1110.0
    assert primary.snapshot()[0] == {"u_l1": 230.0}


def test_on_update_lets_an_above_store_override_its_own_values():
    """The secondary poller's callback must never displace a primary measurement."""
    target = load_registers("config/registers.json").dtsu_target
    primary, secondary = CanonicalStore(), CanonicalStore()
    context = build_context(target, slave_id=1)
    primary.update({"u_l1": 230.0}, ts=1.0)

    on_update = build_on_update(secondary, context, 1, target, above=(primary,))
    on_update({"u_l1": 999.0, "u_l2": 111.0}, ts=5.0)

    u_l1 = context[1].getValues(3, target.points["u_l1"].addr, count=2)
    assert registers_to_float(u_l1, "big", "big") == 2300.0  # primary won
    u_l2 = context[1].getValues(3, target.points["u_l2"].addr, count=2)
    assert registers_to_float(u_l2, "big", "big") == 1110.0
    # the secondary still records what it actually read
    assert secondary.snapshot()[0]["u_l1"] == 999.0


async def test_huawei_poller_does_not_touch_the_watchdog_heartbeat(monkeypatch):
    """A live SmartLogger must not mask a hung ND45 poller from systemd.

    If the secondary poller pinged the heartbeat, a genuinely stuck ND45 poller
    would keep the watchdog satisfied and systemd would never restart the service.
    """
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    stop = asyncio.Event()
    fake = _HuaweiFakeClient()
    pipe = build_pipeline(
        _huawei_config(meter_unit_id=None), load_registers("config/registers.json"),
        stop, client=object(), huawei_client=fake,
    )
    nd45_coro, huawei_coro, supervisor_coro = pipe.coros

    task = asyncio.create_task(huawei_coro)
    for _ in range(20):  # let at least one poll complete
        await asyncio.sleep(0)
        if fake.requests:
            break
    stop.set()
    await task

    assert fake.requests  # the secondary poller really did run
    assert pipe.metrics.huawei_poll_stats.polls_ok >= 1
    # ...and the watchdog heartbeat was never touched by it
    assert pipe.heartbeat.age(now=time.monotonic()) == float("inf")
    nd45_coro.close()
    supervisor_coro.close()


async def test_both_pollers_reach_the_same_datastore_without_erasing_each_other(monkeypatch):
    """End-to-end: ND45 and SmartLogger values coexist in the served datastore.

    The failure this guards against is a second poller whose write blanks the
    first one's registers, which would show up at Sigenergy as a meter that
    flickers between real and zero readings.
    """
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    from nd45_dtsu666.codec import float_to_registers

    registers = load_registers("config/registers.json")
    nd45_image: dict[int, list[int]] = {}
    for addr, value in ((128, -60000.0), (50, 240.0), (818, 50.0)):
        nd45_image[addr] = float_to_registers(value, "big", "big")

    class _Nd45Client:
        async def read_holding_registers(self, address, count, slave=0):
            flat = {}
            for base, regs in nd45_image.items():
                for offset, reg in enumerate(regs):
                    flat[base + offset] = reg

            class _Resp:
                registers = [flat.get(a, 0) for a in range(address, address + count)]

                def isError(self):
                    return False

            return _Resp()

    class _SmartLogger:
        """Reports 1.2345 MW of PV production at register 40525 (I32, gain 1000)."""

        async def read_holding_registers(self, address, count, slave=0):
            raw = (1_234_500).to_bytes(4, "big")
            flat = {40525: int.from_bytes(raw[:2], "big"), 40526: int.from_bytes(raw[2:], "big")}

            class _Resp:
                registers = [flat.get(a, 0) for a in range(address, address + count)]

                def isError(self):
                    return False

            return _Resp()

    stop = asyncio.Event()
    pipe = build_pipeline(
        _huawei_config(meter_unit_id=None), registers, stop,
        client=_Nd45Client(), huawei_client=_SmartLogger(),
    )
    nd45_coro, huawei_coro, supervisor_coro = pipe.coros
    supervisor_coro.close()

    async def _drive(coro):
        task = asyncio.create_task(coro)
        for _ in range(50):
            await asyncio.sleep(0)
        return task

    nd45_task = await _drive(nd45_coro)
    huawei_task = await _drive(huawei_coro)
    stop.set()
    await asyncio.gather(nd45_task, huawei_task)

    # both sources landed in the merged view
    values, _ts = pipe.merged_store.snapshot()
    assert values["p_total"] == pytest.approx(-60000.0)
    assert values["pv_p_total"] == pytest.approx(1_234_500.0)

    # and the ND45 grid measurement is what the DTSU register actually serves
    p_total_point = registers.dtsu_target.points["p_total"]
    regs = pipe.context[pipe.metrics.config.dtsu.slave_id].getValues(
        3, p_total_point.addr, count=2
    )
    served = registers_to_float(regs, "big", "big")
    ct = pipe.metrics.config.dtsu.identity.ir_at
    assert served == pytest.approx(-60000.0 / ct * 10)


def test_fault_reporter_labels_the_source_it_is_reporting_on():
    """A SmartLogger outage must not read as an ND45 fault in the journal."""
    class _FakeLog:
        def __init__(self):
            self.warnings, self.infos = [], []

        def warning(self, msg, *args):
            self.warnings.append(msg % args)

        def info(self, msg, *args):
            self.infos.append(msg % args)

    logger = _FakeLog()
    reporter = FaultReporter(logger=logger, label="SmartLogger", clock=lambda: 0.0)
    reporter.failure(RuntimeError("unreachable"))
    reporter.success()

    assert "SmartLogger polling failed: unreachable" in logger.warnings[0]
    assert "SmartLogger polling recovered" in logger.infos[0]
    assert not any("ND45" in line for line in logger.warnings + logger.infos)
