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
    raw["safety"]["check_interval_s"] = 0.01
    raw["bridges"] = [{**SMARTLOGGER_BRIDGE, **overrides}]
    return AppConfig.model_validate(raw)


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
