import asyncio
import socket
import time

import pytest

from nd45_dtsu666.app import FaultReporter, build_on_update, build_pipeline, connect_with_retry, run_app
from nd45_dtsu666.canonical import CanonicalStore
from nd45_dtsu666.codec import registers_to_float
from nd45_dtsu666.config import AppConfig, load_config, load_registers
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


# --- multiple independent bridges -------------------------------------------


SMARTLOGGER_BRIDGE = {
    "name": "smartlogger",
    "enabled": True,
    "source": {
        "type": "huawei",
        "host": "10.0.0.5",
        "register_map": "huawei_plant_source",
        "poll_interval_s": 5.0,
        "stall_timeout_s": 60.0,
    },
    "dtsu": {
        "transport": "rtu",
        "slave_id": 10,
        "identity": {"ir_at": 200},
        "rtu": {"port": "/dev/ttyAMA3"},
    },
    "safety": {"max_data_age_s": 30.0},
}


class _FakeSourceClient:
    """Duck-typed stand-in: a real AsyncModbusTcpClient needs a running loop."""

    connected = True

    def __init__(self, image: dict[int, int] | None = None):
        self.image = dict(image or {})
        self.requests: list[tuple[int, int, int]] = []
        self.closed = 0

    async def connect(self):
        return True

    async def read_holding_registers(self, address, count, slave=0):
        self.requests.append((address, count, slave))
        image = self.image

        class _Resp:
            registers = [image.get(a, 0) for a in range(address, address + count)]

            def isError(self):
                return False

        return _Resp()

    def close(self):
        self.closed += 1


def _two_bridge_config(**bridge_overrides):
    config = load_config("config/config.json")
    raw = config.model_dump(mode="json", by_alias=True)
    raw["bridges"] = [{**SMARTLOGGER_BRIDGE, **bridge_overrides}]
    return AppConfig.model_validate(raw)


def test_legacy_config_without_bridges_builds_exactly_one_bridge(monkeypatch):
    """Regression guard: nothing about a single-bridge deployment changes."""
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = load_config("config/config.json")
    assert [b.enabled for b in config.bridges] == [False]  # shipped, disabled

    pipe = build_pipeline(
        config, load_registers("config/registers.json"), asyncio.Event(), client=object()
    )

    assert len(pipe.bridges) == 1
    assert pipe.bridges[0].name == "nd45"
    assert len(pipe.coros) == 2  # poller + supervisor, as before
    for coro in pipe.coros:
        coro.close()


def test_two_bridges_get_two_pollers_and_two_supervisors(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    pipe = build_pipeline(
        _two_bridge_config(), load_registers("config/registers.json"), asyncio.Event(),
        clients={"nd45": _FakeSourceClient(), "smartlogger": _FakeSourceClient()},
    )

    assert [b.name for b in pipe.bridges] == ["nd45", "smartlogger"]
    assert len(pipe.coros) == 4
    for coro in pipe.coros:
        coro.close()


def test_bridges_share_nothing(monkeypatch):
    """The isolation this whole design rests on: no store, context or client in common."""
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    pipe = build_pipeline(
        _two_bridge_config(), load_registers("config/registers.json"), asyncio.Event(),
        clients={"nd45": _FakeSourceClient(), "smartlogger": _FakeSourceClient()},
    )
    nd45, smart = pipe.bridges

    assert nd45.store is not smart.store
    assert nd45.context is not smart.context
    assert nd45.client is not smart.client
    assert nd45.heartbeat is not smart.heartbeat
    assert nd45.poll_stats is not smart.poll_stats
    assert nd45.server_status is not smart.server_status
    # ...and each carries its own output config, including the serial port
    assert nd45.spec.dtsu.rtu.port == "/dev/ttyAMA2"
    assert smart.spec.dtsu.rtu.port == "/dev/ttyAMA3"
    # a value written to one store is invisible in the other
    nd45.store.update({"p_total": -60000.0}, ts=1.0)
    assert smart.store.snapshot()[0] == {}
    for coro in pipe.coros:
        coro.close()


def test_each_bridge_uses_its_own_register_map_and_poller(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    registers = load_registers("config/registers.json")
    pipe = build_pipeline(
        _two_bridge_config(), registers, asyncio.Event(),
        clients={"nd45": _FakeSourceClient(), "smartlogger": _FakeSourceClient()},
    )
    nd45, smart = pipe.bridges
    assert nd45.spec.source.register_map == "nd45_source"
    assert smart.spec.source.register_map == "huawei_plant_source"
    assert nd45.spec.source.type == "nd45"
    assert smart.spec.source.type == "huawei"
    for coro in pipe.coros:
        coro.close()


def test_pipeline_accessors_point_at_the_first_bridge(monkeypatch):
    """Single-bridge callers (diag, static, existing tests) keep working."""
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    pipe = build_pipeline(
        _two_bridge_config(), load_registers("config/registers.json"), asyncio.Event(),
        clients={"nd45": _FakeSourceClient(), "smartlogger": _FakeSourceClient()},
    )
    assert pipe.store is pipe.bridges[0].store
    assert pipe.context is pipe.bridges[0].context
    assert pipe.client is pipe.bridges[0].client
    assert pipe.heartbeat is pipe.bridges[0].heartbeat
    assert pipe.bridge("smartlogger") is pipe.bridges[1]
    with pytest.raises(KeyError, match="no bridge named 'nope'"):
        pipe.bridge("nope")
    for coro in pipe.coros:
        coro.close()


def test_build_pipeline_rejects_a_missing_register_section(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    registers = load_registers("config/registers.json")
    stripped = registers.model_copy(update={"huawei_plant_source": None})
    with pytest.raises(ValueError, match="no source section 'huawei_plant_source'"):
        build_pipeline(
            _two_bridge_config(), stripped, asyncio.Event(),
            clients={"nd45": _FakeSourceClient(), "smartlogger": _FakeSourceClient()},
        )


async def test_each_bridge_writes_only_its_own_datastore(monkeypatch):
    """End-to-end: two sources, two datastores, no cross-contamination."""
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    from nd45_dtsu666.codec import float_to_registers

    registers = load_registers("config/registers.json")
    # ND45 image: p_total (float32 at 128) = -60 kW
    nd45_image = {}
    for offset, reg in enumerate(float_to_registers(-60000.0, "big", "big")):
        nd45_image[128 + offset] = reg
    # SmartLogger image: Active power (I32 at 40525, gain 1000 kW) = 1.2345 MW
    raw = (1_234_500).to_bytes(4, "big")
    smart_image = {
        40525: int.from_bytes(raw[:2], "big"),
        40526: int.from_bytes(raw[2:], "big"),
    }

    stop = asyncio.Event()
    pipe = build_pipeline(
        _two_bridge_config(source={**SMARTLOGGER_BRIDGE["source"], "poll_interval_s": 0.01}),
        registers, stop,
        clients={
            "nd45": _FakeSourceClient(nd45_image),
            "smartlogger": _FakeSourceClient(smart_image),
        },
    )
    # drive only the pollers; close the output supervisors
    pollers = [pipe.coros[0], pipe.coros[2]]
    pipe.coros[1].close()
    pipe.coros[3].close()

    tasks = [asyncio.create_task(c) for c in pollers]
    for _ in range(60):
        await asyncio.sleep(0)
        if all(b.store.snapshot()[1] == b.store.snapshot()[1] for b in pipe.bridges) and all(
            b.store.snapshot()[0] for b in pipe.bridges
        ):
            break
    stop.set()
    await asyncio.gather(*tasks)

    nd45, smart = pipe.bridges
    assert nd45.store.snapshot()[0]["p_total"] == pytest.approx(-60000.0)
    assert smart.store.snapshot()[0]["p_total"] == pytest.approx(1_234_500.0)

    # each bridge's DTSU register holds its own value, divided by its own CT ratio
    point = registers.dtsu_target.points["p_total"]
    for bridge, expected in ((nd45, -60000.0), (smart, 1_234_500.0)):
        regs = bridge.context[bridge.spec.dtsu.slave_id].getValues(3, point.addr, count=2)
        served = registers_to_float(regs, "big", "big")
        ct = bridge.spec.dtsu.identity.ir_at
        assert served == pytest.approx(expected / ct * 10)


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
