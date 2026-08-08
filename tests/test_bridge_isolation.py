"""The property the two-bridge design rests on: bridges fail independently.

One source going dark must silence that bridge's output and nothing else. This is
the whole reason each bridge carries its own store, gate, datastore and transport,
and it cannot be checked by reading the wiring -- it needs the real supervisor
driven against a fake clock, the same pattern as test_server.py.

No real serial port or socket is opened (CLAUDE.md): output servers are injected
via `server_factory` and time comes from a dict.
"""

from __future__ import annotations

import asyncio

import pytest

from nd45_dtsu666.app import build_pipeline, supervise_poller
from nd45_dtsu666.canonical import HealthGate
from nd45_dtsu666.config import AppConfig, load_config, load_registers
from nd45_dtsu666.dtsu_server import supervise_server
from nd45_dtsu666.etango_poller import DeviceLink, MultiHostClient
from nd45_dtsu666.metrics import ServerStatus

REGISTERS = load_registers("config/registers.json")


def _close_coros(pipe) -> None:
    """Close build_pipeline's coroutines a test drives by hand instead.

    Avoids "coroutine was never awaited" warnings; these tests run
    supervise_poller directly so they can inject a clock.
    """
    for coro in pipe.coros:
        coro.close()


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
    "safety": {"max_data_age_s": 30.0, "check_interval_s": 0.01},
}


def two_bridge_config(**overrides) -> AppConfig:
    raw = load_config("config/config.json").model_dump(mode="json", by_alias=True)
    # Tick both supervisors fast so a test does not wait half a second per round.
    raw["bridges"][0]["safety"]["check_interval_s"] = 0.01
    raw["bridges"] = [raw["bridges"][0], {**SMARTLOGGER_BRIDGE, **overrides}]
    return AppConfig.model_validate(raw)


ETANGO_BRIDGE = {
    "name": "etango",
    "enabled": True,
    "source": {
        "type": "etango",
        "register_map": "etango_source",
        "poll_interval_s": 2.0,
        "stall_timeout_s": 30.0,
        "devices": [
            {"host": "192.168.30.5", "unit_id": 1, "aggregate": True},
            {"host": "192.168.30.7", "unit_id": 1, "aggregate": True},
            {"host": "192.168.30.9", "unit_id": 1, "aggregate": True},
            {"host": "192.168.30.11", "unit_id": 1, "aggregate": True},
        ],
    },
    "dtsu": {
        "transport": "rtu",
        "slave_id": 10,
        "identity": {"ir_at": 200},
        "rtu": {"port": "/dev/ttyAMA5"},
    },
    "safety": {"max_data_age_s": 10.0, "check_interval_s": 0.01},
}


def three_bridge_config(**overrides) -> AppConfig:
    raw = load_config("config/config.json").model_dump(mode="json", by_alias=True)
    raw["bridges"][0]["safety"]["check_interval_s"] = 0.01
    raw["bridges"] = [
        raw["bridges"][0], SMARTLOGGER_BRIDGE, {**ETANGO_BRIDGE, **overrides},
    ]
    return AppConfig.model_validate(raw)


class FakeEtangoDeviceClient:
    """FC04 stand-in for one e2TANGO controller (see FakeSourceClient)."""

    connected = True

    def __init__(self):
        self.requests = 0
        self.closed = 0

    async def connect(self):
        return True

    async def read_input_registers(self, address, count, slave=0):
        self.requests += 1

        class _Resp:
            registers = [0] * count

            def isError(self):
                return False

        return _Resp()

    def close(self):
        self.closed += 1


def fake_multi_host_client() -> MultiHostClient:
    return MultiHostClient([
        DeviceLink(client=FakeEtangoDeviceClient(), unit_id=1, aggregate=True, host=host)
        for host in ("192.168.30.5", "192.168.30.7", "192.168.30.9", "192.168.30.11")
    ])


class HangingEtangoDeviceClient:
    """FC04 stand-in whose reads never return (see HangingClient)."""

    connected = True

    def __init__(self):
        self.closed = 0
        self.calls = 0

    async def connect(self):
        return True

    async def read_input_registers(self, address, count, slave=0):
        self.calls += 1
        await asyncio.Event().wait()  # never returns

    def close(self):
        self.closed += 1


def hanging_multi_host_client() -> MultiHostClient:
    return MultiHostClient([
        DeviceLink(client=HangingEtangoDeviceClient(), unit_id=1, aggregate=True, host=host)
        for host in ("192.168.30.5", "192.168.30.7", "192.168.30.9", "192.168.30.11")
    ])


class FakeServer:
    """Duck-typed stand-in for a pymodbus server (see test_server.py)."""

    def __init__(self, name: str):
        self.name = name
        self.serving = False
        self.starts = 0

    async def serve_forever(self):
        self.serving = True
        self.starts += 1
        try:
            await asyncio.Event().wait()
        finally:
            self.serving = False

    async def shutdown(self):
        self.serving = False

    def is_active(self):
        return True


class FakeSourceClient:
    connected = True

    def __init__(self):
        self.requests = 0
        self.closed = 0

    async def connect(self):
        return True

    async def read_holding_registers(self, address, count, slave=0):
        self.requests += 1

        class _Resp:
            registers = [0] * count

            def isError(self):
                return False

        return _Resp()

    def close(self):
        self.closed += 1


async def _run_two_supervisors(config, ages: dict[str, float], clock: dict):
    """Drive one supervise_server per bridge against injected clocks and stores.

    `ages` maps bridge name -> the data age its store should report, so a test can
    age one bridge's data without touching the other's.
    """
    stop = asyncio.Event()
    servers: dict[str, FakeServer] = {}
    statuses: dict[str, ServerStatus] = {}
    tasks = []

    for spec in config.bridge_specs:
        server = FakeServer(spec.name)
        status = ServerStatus()
        servers[spec.name] = server
        statuses[spec.name] = status

        class _Store:
            def __init__(self, name):
                self._name = name

            def age(self, now):
                return ages[self._name]

        tasks.append(
            asyncio.create_task(
                supervise_server(
                    spec.dtsu, object(), _Store(spec.name),
                    HealthGate(spec.safety.max_data_age_s),
                    0.01, stop,
                    server_factory=lambda s=server: s,
                    now=lambda: clock["t"],
                    status=status,
                )
            )
        )
    return stop, servers, statuses, tasks


async def _settle(seconds: float = 0.12) -> None:
    """Let the supervisors' real `wait_for` timeouts actually fire.

    asyncio.sleep(0) only yields; these loops wake on a timeout, so the tests
    need a little real time. Intervals in the fixtures are set to ~10ms to keep
    that cheap -- the clocks that matter (data age, stall age) are still injected.
    """
    await asyncio.sleep(seconds)


async def test_a_stale_smartlogger_silences_only_its_own_output():
    """The headline requirement: a dead SmartLogger must not touch the ND45 bus."""
    config = two_bridge_config()
    clock = {"t": 1000.0}
    # ND45 fresh (0.5s < 3.0s), SmartLogger long gone (120s > 30.0s)
    ages = {"nd45": 0.5, "smartlogger": 120.0}
    stop, servers, statuses, tasks = await _run_two_supervisors(config, ages, clock)
    try:
        await _settle()
        assert statuses["nd45"].running is True
        assert servers["nd45"].serving is True
        assert statuses["smartlogger"].running is False
        assert servers["smartlogger"].serving is False
    finally:
        stop.set()
        await asyncio.gather(*tasks)


async def test_a_stale_nd45_silences_only_its_own_output():
    """The converse: the ND45 bridge failing must not take the PV bridge down."""
    config = two_bridge_config()
    clock = {"t": 1000.0}
    ages = {"nd45": 120.0, "smartlogger": 2.0}
    stop, servers, statuses, tasks = await _run_two_supervisors(config, ages, clock)
    try:
        await _settle()
        assert statuses["nd45"].running is False
        assert statuses["smartlogger"].running is True
    finally:
        stop.set()
        await asyncio.gather(*tasks)


async def test_both_serve_while_both_sources_are_fresh():
    config = two_bridge_config()
    clock = {"t": 1000.0}
    ages = {"nd45": 0.4, "smartlogger": 6.0}
    stop, servers, statuses, tasks = await _run_two_supervisors(config, ages, clock)
    try:
        await _settle()
        assert statuses["nd45"].running is True
        assert statuses["smartlogger"].running is True
    finally:
        stop.set()
        await asyncio.gather(*tasks)


async def test_each_bridge_uses_its_own_threshold_not_a_shared_one():
    """20s stale: past the ND45's 3.0s, well inside the SmartLogger's 30.0s.

    A single shared safety.max_data_age_s would park the SmartLogger bridge in
    permanent fail-safe, since it only polls every 5s.
    """
    config = two_bridge_config()
    clock = {"t": 1000.0}
    ages = {"nd45": 20.0, "smartlogger": 20.0}
    stop, servers, statuses, tasks = await _run_two_supervisors(config, ages, clock)
    try:
        await _settle()
        assert statuses["nd45"].running is False
        assert statuses["smartlogger"].running is True
    finally:
        stop.set()
        await asyncio.gather(*tasks)


async def test_a_recovering_source_brings_only_its_own_output_back():
    config = two_bridge_config()
    clock = {"t": 1000.0}
    ages = {"nd45": 0.5, "smartlogger": 120.0}
    stop, servers, statuses, tasks = await _run_two_supervisors(config, ages, clock)
    try:
        await _settle()
        assert statuses["smartlogger"].running is False
        nd45_starts = statuses["nd45"].starts

        ages["smartlogger"] = 1.0  # SmartLogger comes back
        clock["t"] += 100.0  # past min_restart_interval
        await _settle()

        assert statuses["smartlogger"].running is True
        # the ND45 bridge was never restarted as a side effect
        assert statuses["nd45"].starts == nd45_starts
        assert statuses["nd45"].stops == 0
    finally:
        stop.set()
        await asyncio.gather(*tasks)


# --- per-bridge stall recovery ----------------------------------------------


class HangingClient:
    """Reads never return, simulating a wedged await inside pymodbus."""

    connected = True

    def __init__(self):
        self.closed = 0
        self.calls = 0

    async def connect(self):
        return True

    async def read_holding_registers(self, address, count, slave=0):
        self.calls += 1
        await asyncio.Event().wait()  # never returns

    def close(self):
        self.closed += 1


async def test_a_stalled_poll_loop_is_rebuilt_with_a_fresh_client(monkeypatch):
    """Recovery from a hung await, which the systemd watchdog no longer covers.

    A process restart would take every sibling bridge down, so a wedged poll loop
    is torn down and rebuilt in place instead.
    """
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = two_bridge_config()
    stop = asyncio.Event()
    first = HangingClient()
    built: list[HangingClient] = []
    pipe = build_pipeline(
        config, REGISTERS, stop,
        clients={"nd45": FakeSourceClient(), "smartlogger": first},
    )
    _close_coros(pipe)
    smart = pipe.bridge("smartlogger")

    def factory():
        client = HangingClient()
        built.append(client)
        return client

    smart.client_factory = factory
    clock = {"t": 0.0}

    async def _no_connect(client, stop_event, *args, **kwargs):
        return True

    task = asyncio.create_task(
        supervise_poller(
            smart, REGISTERS, stop,
            on_update=lambda values, ts: None,
            on_error=lambda exc: None,
            now=lambda: clock["t"],
            connect=_no_connect,
        )
    )
    try:
        await _settle()
        assert first.calls == 1  # the poll started and then hung
        assert smart.recovery.restarts == 0

        clock["t"] = 61.0  # past stall_timeout_s of 60.0
        await _settle(0.3)

        assert smart.recovery.restarts >= 1
        assert first.closed == 1  # the wedged client was closed
        assert built, "a fresh client was built"
        assert smart.client is built[-1]
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


async def test_a_healthy_poll_loop_is_never_rebuilt(monkeypatch):
    """A source that is merely unreachable keeps cycling and must be left alone.

    run_poller already survives that by touching the heartbeat from its error
    path, so recovery must not fire and churn the client pointlessly.
    """
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = two_bridge_config()
    stop = asyncio.Event()

    class _BrokenClient:
        connected = False

        async def connect(self):
            return False

        async def read_holding_registers(self, address, count, slave=0):
            raise RuntimeError("smartlogger unreachable")

        def close(self):
            pass

    pipe = build_pipeline(
        config, REGISTERS, stop,
        clients={"nd45": FakeSourceClient(), "smartlogger": _BrokenClient()},
    )
    _close_coros(pipe)
    smart = pipe.bridge("smartlogger")
    errors: list[Exception] = []
    clock = {"t": 0.0}

    def on_error(exc):
        errors.append(exc)
        smart.heartbeat.touch(clock["t"])  # what build_pipeline's callback does

    task = asyncio.create_task(
        supervise_poller(
            smart, REGISTERS, stop,
            on_update=lambda values, ts: None,
            on_error=on_error,
            now=lambda: clock["t"],
        )
    )
    try:
        await _settle(0.15)
        assert errors, "the failing poll reached on_error"
        clock["t"] = 59.0  # still inside stall_timeout_s
        await _settle(0.15)
        assert smart.recovery.restarts == 0
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


async def test_recovering_one_bridge_leaves_the_other_polling(monkeypatch):
    """A rebuild is scoped to its own bridge: the sibling keeps making progress.

    Each bridge gets its own injected clock, so only the SmartLogger's stall age
    advances -- otherwise a shared clock would trip both stall timeouts at once
    and the test would prove nothing about isolation.
    """
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = two_bridge_config()
    stop = asyncio.Event()
    nd45_client = FakeSourceClient()
    pipe = build_pipeline(
        config, REGISTERS, stop,
        clients={"nd45": nd45_client, "smartlogger": HangingClient()},
    )
    _close_coros(pipe)
    nd45, smart = pipe.bridges
    smart.client_factory = HangingClient
    smart_clock = {"t": 0.0}
    nd45_clock = {"t": 0.0}

    async def _no_connect(client, stop_event, *args, **kwargs):
        return True

    stalled = asyncio.create_task(
        supervise_poller(
            smart, REGISTERS, stop,
            on_update=lambda values, ts: None, on_error=lambda exc: None,
            now=lambda: smart_clock["t"], connect=_no_connect,
        )
    )
    healthy = asyncio.create_task(
        supervise_poller(
            nd45, REGISTERS, stop,
            on_update=lambda values, ts: nd45.heartbeat.touch(nd45_clock["t"]),
            on_error=lambda exc: None,
            now=lambda: nd45_clock["t"], connect=_no_connect,
        )
    )
    try:
        await _settle(0.12)
        before = nd45_client.requests
        smart_clock["t"] = 61.0  # only the SmartLogger bridge stalls
        await _settle(0.3)

        assert smart.recovery.restarts >= 1  # the PV bridge was rebuilt
        assert nd45.recovery.restarts == 0  # the ND45 bridge was untouched
        assert nd45_client.requests > before  # ...and kept polling throughout
    finally:
        stop.set()
        await asyncio.gather(stalled, healthy)


async def test_a_poll_loop_that_dies_is_restarted(monkeypatch):
    """run_poller should never return early; if it does, the bridge must not go dark.

    Defensive branch: run_poller swallows Exception itself, so only a bug in the
    loop could end it. Left unhandled that bridge would stop polling silently
    while its sibling carried on, which is exactly the failure that is hard to
    spot in the field.
    """
    import nd45_dtsu666.app as app_mod

    config = two_bridge_config()
    stop = asyncio.Event()
    pipe = build_pipeline(
        config, REGISTERS, stop,
        clients={"nd45": FakeSourceClient(), "smartlogger": FakeSourceClient()},
    )
    _close_coros(pipe)
    smart = pipe.bridge("smartlogger")
    smart.client_factory = FakeSourceClient
    clock = {"t": 0.0}
    starts = {"n": 0}

    async def _dying_poller(*args, **kwargs):
        starts["n"] += 1
        return  # returns without stop_event being set -- the bug being guarded

    monkeypatch.setattr(app_mod, "run_poller", _dying_poller)

    async def _no_connect(client, stop_event, *args, **kwargs):
        return True

    task = asyncio.create_task(
        supervise_poller(
            smart, REGISTERS, stop,
            on_update=lambda values, ts: None, on_error=lambda exc: None,
            now=lambda: clock["t"], connect=_no_connect,
        )
    )
    try:
        await _settle(0.2)
        assert smart.recovery.restarts >= 1
        assert starts["n"] >= 2  # it was actually restarted, not just counted
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


def test_config_rejects_two_bridges_on_one_serial_port():
    """The most dangerous misconfiguration: pymodbus swallows the bind error.

    Two RTU servers on one /dev/tty* would fight over the device, and
    pymodbus 3.6.9's listen() hides the OSError, so the loser hangs silently
    rather than failing. Reject it at load time.
    """
    with pytest.raises(ValueError, match="both serve RTU on /dev/ttyAMA2"):
        two_bridge_config(
            dtsu={
                "transport": "rtu",
                "slave_id": 10,
                "rtu": {"port": "/dev/ttyAMA2"},  # same port as the ND45 bridge
            }
        )


# --- three bridges: nd45 + smartlogger + etango -----------------------------


async def test_a_stale_etango_bridge_silences_only_its_own_output():
    """The third source type must fail independently too, same as SmartLogger."""
    config = three_bridge_config()
    clock = {"t": 1000.0}
    ages = {"nd45": 0.5, "smartlogger": 2.0, "etango": 60.0}
    stop, servers, statuses, tasks = await _run_two_supervisors(config, ages, clock)
    try:
        await _settle()
        assert statuses["nd45"].running is True
        assert statuses["smartlogger"].running is True
        assert statuses["etango"].running is False
        assert servers["etango"].serving is False
    finally:
        stop.set()
        await asyncio.gather(*tasks)


async def test_build_pipeline_accepts_an_injected_multi_host_client():
    """build_pipeline's `clients=` injection must accept an etango-shaped client
    (MultiHostClient wrapping several fakes), not just a single-host fake."""
    config = three_bridge_config()
    stop = asyncio.Event()
    etango_client = fake_multi_host_client()
    pipe = build_pipeline(
        config, REGISTERS, stop,
        clients={
            "nd45": FakeSourceClient(),
            "smartlogger": FakeSourceClient(),
            "etango": etango_client,
        },
    )
    _close_coros(pipe)
    etango = pipe.bridge("etango")
    assert etango.client is etango_client
    assert len(etango.client.devices) == 4


async def test_recovering_the_etango_bridge_leaves_its_siblings_polling(monkeypatch):
    """A rebuild of the third bridge must not disturb the other two."""
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = three_bridge_config()
    stop = asyncio.Event()
    nd45_client = FakeSourceClient()
    pipe = build_pipeline(
        config, REGISTERS, stop,
        clients={
            "nd45": nd45_client,
            "smartlogger": FakeSourceClient(),
            "etango": hanging_multi_host_client(),
        },
    )
    _close_coros(pipe)
    nd45, _smart, etango = pipe.bridges
    etango.client_factory = hanging_multi_host_client
    etango_clock = {"t": 0.0}
    nd45_clock = {"t": 0.0}

    async def _no_connect(client, stop_event, *args, **kwargs):
        return True

    stalled = asyncio.create_task(
        supervise_poller(
            etango, REGISTERS, stop,
            on_update=lambda values, ts: None, on_error=lambda exc: None,
            now=lambda: etango_clock["t"], connect=_no_connect,
        )
    )
    healthy = asyncio.create_task(
        supervise_poller(
            nd45, REGISTERS, stop,
            on_update=lambda values, ts: nd45.heartbeat.touch(nd45_clock["t"]),
            on_error=lambda exc: None,
            now=lambda: nd45_clock["t"], connect=_no_connect,
        )
    )
    try:
        await _settle(0.12)
        before = nd45_client.requests
        etango_clock["t"] = 31.0  # only the etango bridge stalls (stall_timeout_s=30.0)
        await _settle(0.3)

        assert etango.recovery.restarts >= 1
        assert nd45.recovery.restarts == 0
        assert nd45_client.requests > before
    finally:
        stop.set()
        await asyncio.gather(stalled, healthy)


async def test_recovery_reconnect_keeps_the_watchdog_heartbeat_alive(monkeypatch):
    """A source unreachable *during* stall recovery must not restart the process.

    supervise_poller rebuilds the client and then reconnects. If that reconnect
    runs without touching the heartbeat, an unreachable source keeps it retrying
    with backoff forever while SlowestHeartbeat ages without bound -- and at
    WatchdogSec (90s on this deployment) systemd kills the service. That is the
    exact escalation this supervisor exists to prevent: an unreachable source is
    not a stall, and the freshness gate alone should handle it.

    Measured before the fix: heartbeat age tracked elapsed time 1:1, reaching
    150s while the reconnect was still retrying.
    """
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = two_bridge_config()
    stop = asyncio.Event()
    pipe = build_pipeline(
        config, REGISTERS, stop,
        clients={"nd45": FakeSourceClient(), "smartlogger": HangingClient()},
    )
    _close_coros(pipe)
    smart = pipe.bridge("smartlogger")
    smart.client_factory = HangingClient
    clock = {"t": 0.0}
    seen_heartbeats: list[object] = []

    async def _never_connects(client, stop_event, *args, heartbeat=None, **kwargs):
        """What connect_with_retry does against a dead source: retry forever."""
        seen_heartbeats.append(heartbeat)
        while not stop_event.is_set():
            if heartbeat is not None:
                heartbeat.touch(clock["t"])
            clock["t"] += 10.0  # 10s of backoff per attempt
            await asyncio.sleep(0)
            if clock["t"] > 300.0:
                return False
        return False

    task = asyncio.create_task(
        supervise_poller(
            smart, REGISTERS, stop,
            on_update=lambda values, ts: None,
            on_error=lambda exc: None,
            now=lambda: clock["t"],
            connect=_never_connects,
        )
    )
    try:
        await _settle()
        clock["t"] = 61.0  # past the SmartLogger bridge's 60s stall_timeout_s
        await _settle(0.3)

        assert seen_heartbeats, "recovery never reached the reconnect"
        assert seen_heartbeats[0] is smart.heartbeat, (
            "supervise_poller must pass the bridge's heartbeat to connect_with_retry"
        )
        # The watchdog reads exactly this; 90s is WatchdogSec on the deployment.
        assert smart.heartbeat.age(clock["t"]) <= 90.0
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


# --- MQTT publishing is per bridge too ---


def test_each_bridge_publishes_under_its_own_topics():
    from nd45_dtsu666.config import MqttConf
    from nd45_dtsu666.mqtt_publisher import build_source

    config = load_config("config/config.json")
    source = build_source(MqttConf(topic_prefix="p"), _publish_runtimes(config))
    topics = {b.name: b.measurement_topic for b in source.bridges}
    assert len(set(topics.values())) == len(topics), "two bridges share a topic"
    for name, topic in topics.items():
        assert topic == f"p/{name}/measurements"


def test_each_bridge_publishes_against_its_own_freshness_threshold():
    """ND45 tolerates 3s, the SmartLogger alternative 30s -- the same split the
    fail-safe uses, so MQTT goes quiet exactly when that bridge's output does."""
    from nd45_dtsu666.config import MqttConf
    from nd45_dtsu666.mqtt_publisher import build_source

    config = load_config("config/config.json")
    source = build_source(MqttConf(), _publish_runtimes(config))
    for bridge, spec in zip(source.bridges, config.bridge_specs):
        assert bridge.gate.max_age == spec.safety.max_data_age_s


def test_a_stale_bridge_does_not_silence_its_siblings_payload():
    from nd45_dtsu666.config import MqttConf
    from nd45_dtsu666.mqtt_publisher import build_payload, build_source

    config = load_config("config/config.json")
    source = build_source(MqttConf(), _publish_runtimes(config))
    fresh, stale = source.bridges[0], source.bridges[1]
    fresh.store.update({"p_total": 42.0}, 100.0)  # stale's store is never fed
    assert build_payload(fresh, ["p_total"], 100.0, 0.0) is not None
    assert build_payload(stale, ["p_total"], 100.0, 0.0) is None


def test_two_systemd_instances_present_different_client_ids_and_will_topics():
    """One connection per client id: a shared one has the broker evict each
    instance in turn, forever -- the MQTT shape of the prometheus.port collision."""
    from nd45_dtsu666.config import MqttConf
    from nd45_dtsu666.mqtt_publisher import build_source

    config = load_config("config/config.json")
    runtimes = _publish_runtimes(config)
    conf = MqttConf()
    first = build_source(conf, runtimes, only=config.bridge_specs[0].name)
    second = build_source(conf, runtimes, only=config.bridge_specs[1].name)
    assert first.client_id != second.client_id
    assert first.availability_topic != second.availability_topic


class _PublishRuntime:
    """Stand-in for app.BridgeRuntime: build_source reads name, spec and store."""

    def __init__(self, spec) -> None:
        from nd45_dtsu666.canonical import CanonicalStore

        self.name = spec.name
        self.spec = spec
        self.store = CanonicalStore()


def _publish_runtimes(config):
    return [_PublishRuntime(spec) for spec in config.bridge_specs]
