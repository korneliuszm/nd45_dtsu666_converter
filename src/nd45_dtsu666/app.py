"""Orchestration: wire one or more independent bridges under a single asyncio loop.

A *bridge* is a complete source -> DTSU666 translator: its own upstream client,
canonical store, served datastore, output transport and fail-safe. Bridges share
the event loop and the Prometheus endpoint and nothing else, so one source going
dark silences that bridge's output alone.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pymodbus.client import AsyncModbusTcpClient

from . import huawei_poller, metrics, nd45_poller
from .canonical import CanonicalStore, HealthGate, SampleStats
from .config import AppConfig, BridgeConf, RegisterMap
from .dtsu_server import (
    RtuActivity,
    WireActivity,
    build_context,
    supervise_server,
    update_datastore,
)
from .metrics import BridgeMetrics, MetricsSource, PollStats, RecoveryStats, ServerStatus
from .nd45_poller import run_poller
from .watchdog import Heartbeat, notify_ready, watchdog_loop, watchdog_seconds

log = logging.getLogger(__name__)

# The only place a source type is dispatched on. Both poll_once functions share
# the signature (client, source, slave, overrange_seen=None), so run_poller can
# drive either one.
# Points whose short-term spread the monitor reports. The signed flows plus the
# two inputs they are computed from, so an unstable P can be traced to U, I or
# neither.
SPREAD_POINTS = ("p_total", "p_l1", "p_l2", "p_l3", "pf_total", "u_l1", "i_l1")

_POLL_ONCE: dict[str, Callable] = {
    "nd45": nd45_poller.poll_once,
    "huawei": huawei_poller.poll_once,
}


@dataclass
class BridgeRuntime:
    """Everything one bridge owns at runtime. Nothing here is shared."""

    name: str
    spec: BridgeConf
    store: CanonicalStore
    context: object
    client: object
    # Rebuilds the upstream client after a hung poll loop is torn down.
    client_factory: Callable[[], object]
    heartbeat: Heartbeat
    poll_stats: PollStats
    server_status: ServerStatus
    recovery: RecoveryStats
    # Rolling spread of the last few seconds of samples. The store holds only the
    # latest value, which cannot answer the question a jittering meter raises:
    # is the swing in the measurement, or in how we read it?
    spread: SampleStats = field(default_factory=lambda: SampleStats(SPREAD_POINTS))
    activity: RtuActivity | None = None
    # Raw RS-485 traffic on this bridge's output port, counted before pymodbus
    # filters by slave id -- the only way to tell "nobody is polling this bus"
    # apart from "someone is polling it under a different address".
    wire: WireActivity | None = None

    def replace_client(self) -> object:
        self.client = self.client_factory()
        return self.client


@dataclass
class Pipeline:
    """Assembled bridges plus the coroutines that run them."""

    bridges: list[BridgeRuntime]
    coros: list
    metrics: MetricsSource | None = None

    @property
    def watchdog_heartbeat(self) -> "SlowestHeartbeat":
        """What the systemd watchdog watches: the least-progressing poll loop."""
        return SlowestHeartbeat(bridge.heartbeat for bridge in self.bridges)

    # Single-bridge accessors, for callers that operate on one bridge (monitor's
    # register table, rtudebug, diag, static) and for existing tests.
    @property
    def primary(self) -> BridgeRuntime:
        return self.bridges[0]

    @property
    def store(self) -> CanonicalStore:
        return self.primary.store

    @property
    def context(self) -> object:
        return self.primary.context

    @property
    def client(self) -> object:
        return self.primary.client

    @property
    def heartbeat(self) -> Heartbeat:
        return self.primary.heartbeat

    def bridge(self, name: str) -> BridgeRuntime:
        for bridge in self.bridges:
            if bridge.name == name:
                return bridge
        available = ", ".join(b.name for b in self.bridges)
        raise KeyError(f"no bridge named {name!r} (have: {available})")


def build_on_update(
    store, context, slave_id, target, ct_ratio: float = 1.0
) -> Callable[[dict, float], None]:
    def on_update(values: dict[str, float], ts: float) -> None:
        # Encode into the served datastore BEFORE stamping the store fresh: if
        # a write ever fails, the store stays stale so the freshness fail-safe
        # silences the output, instead of serving a half-written datastore to
        # Sigenergy as if it were fresh.
        update_datastore(context, slave_id, values, target, ct_ratio=ct_ratio)
        store.update(values, ts)

    return on_update


class FaultReporter:
    """Logs poll faults on state transitions, not once per failed poll.

    A sustained outage would otherwise emit a warning every poll interval
    (~200/min). This logs the first failure, a periodic summary, and recovery.

    `label` names the bridge in the log line, so one bridge's outage is not
    misread as another's while triaging from the journal.
    """

    def __init__(
        self,
        logger=log,
        summary_interval: float = 60.0,
        clock=time.monotonic,
        label: str = "ND45",
    ) -> None:
        self._log = logger
        self._summary_interval = summary_interval
        self._clock = clock
        self._label = label
        self._failing = False
        self._count = 0
        self._last_summary = 0.0

    def failure(self, exc: Exception) -> None:
        now = self._clock()
        self._count += 1
        if not self._failing:
            self._failing = True
            self._last_summary = now
            self._log.warning(
                "%s polling failed: %s (retrying; repeats muted)", self._label, exc
            )
        elif now - self._last_summary >= self._summary_interval:
            self._last_summary = now
            self._log.warning(
                "%s still failing: %d polls failed so far", self._label, self._count
            )

    def success(self) -> None:
        if self._failing:
            self._log.info(
                "%s polling recovered after %d failed poll(s)", self._label, self._count
            )
        self._failing = False
        self._count = 0


async def connect_with_retry(
    client, stop_event: asyncio.Event, delay: float = 1.0, max_delay: float = 30.0,
    heartbeat: Heartbeat | None = None,
) -> bool:
    """Keep attempting the initial connect (backoff) until success or stop.

    pymodbus auto-reconnects a link that was up and dropped, but NOT an initial
    connect that never succeeded (e.g. the service starting before the source is
    reachable). This retry loop covers that startup race. Returns True once
    connected, or False if `stop_event` is set before any connection is made.
    """
    current = delay
    while not stop_event.is_set():
        if heartbeat is not None:
            heartbeat.touch(time.monotonic())
        if await client.connect():
            return True
        log.warning("source not reachable; retrying connect in %.1fs", current)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=current)
        except asyncio.TimeoutError:
            pass
        current = min(current * 2, max_delay)
    return False


def _make_client_factory(spec: BridgeConf) -> Callable[[], object]:
    source = spec.source

    def factory():
        return AsyncModbusTcpClient(
            source.host, port=source.port, timeout=source.timeout_s
        )

    return factory


def build_bridge(
    spec: BridgeConf,
    registers: RegisterMap,
    activity: RtuActivity | None = None,
    client=None,
    attended: bool = False,
) -> BridgeRuntime:
    """Build one bridge's runtime state: store, datastore context, client.

    `attended` auto-creates an RtuActivity for THIS bridge when `activity` is
    None, so a sibling bridge under an attended session (monitor/rtudebug) gets
    its own recorder too -- see the incident note below for why this must never
    happen for the unattended production `run` path, regardless of
    `config.prometheus.enabled`.
    """
    source_side = registers.source_by_name(spec.source.register_map)
    if spec.source.type == "nd45":
        nd45_poller.validate_source_coverage(source_side)
    else:
        huawei_poller.validate_source_coverage(source_side)

    # The metrics endpoint normally reports what Sigenergy actually reads, via an
    # RtuActivity. Auto-creating one here is now gated on `attended`, NOT on
    # `config.prometheus.enabled` (2026-08-04 incident):
    # RecordingSlaveContext.getValues (which RtuActivity needs) runs synchronously
    # on every register read Sigenergy makes, on the SAME event loop as this
    # bridge's source poller. Reproduced live on this host, isolated by direct A/B
    # with the same bridge/live source/output server (WireActivity/tracing_rtu_framer
    # ruled OUT the same way -- corruption persists with wire=None): with real,
    # sustained RS-485 read traffic on the port (Sigenergy polling at its normal
    # rate, ~15-20 req/s here), attaching RtuActivity alone was enough to corrupt
    # the concurrently-running ND45 TCP source reads -- p_total swinging between
    # roughly +200kW and -290kW within seconds, absent any read/decode error, and
    # not self-correcting. Clean with activity=None every time; corrupted with it
    # attached every time. Root cause inside pymodbus 3.6.9 not yet identified.
    # `run` (the unattended production path, which is what feeds Sigenergy, and
    # never passes `attended=True`) must not silently do this to a live meter
    # reading. `monitor`/`rtudebug` pass `attended=True` (an attended bring-up
    # session, never the production service) precisely so every bridge still gets
    # its own recorder there -- those callers are choosing to accept this known
    # corruption risk on THEIR panel for a short session. See
    # /persistence/nd45-dtsu666-DEPLOY.md.
    if activity is None and attended:
        activity = RtuActivity()
    # Only RTU has a wire to listen to; a TCP output either accepts a connection
    # or does not, which is already visible.
    wire = WireActivity() if activity is not None and spec.dtsu.transport == "rtu" else None

    context = build_context(
        [side for _name, side in registers.targets],
        spec.dtsu.slave_id,
        activity=activity,
        dtsu_cfg=spec.dtsu,
        sigen_identity=registers.dtsu_sigen_identity,
    )
    factory = _make_client_factory(spec)
    return BridgeRuntime(
        name=spec.name,
        spec=spec,
        store=CanonicalStore(),
        context=context,
        client=client if client is not None else factory(),
        client_factory=factory,
        heartbeat=Heartbeat(),
        poll_stats=PollStats(),
        server_status=ServerStatus(),
        recovery=RecoveryStats(),
        activity=activity,
        wire=wire,
    )


def select_specs(config: AppConfig, only: str | None) -> list[BridgeConf]:
    """Bridges this process should run: all of them, or just the named one.

    Raises rather than silently running nothing, so a typo in a systemd unit fails
    the unit instead of leaving a bus unserved.
    """
    specs = config.bridge_specs
    if only is None:
        return specs
    selected = [spec for spec in specs if spec.name == only]
    if not selected:
        available = ", ".join(spec.name for spec in specs)
        raise SystemExit(f"unknown bridge {only!r} (enabled bridges: {available})")
    return selected


def build_pipeline(
    config: AppConfig,
    registers: RegisterMap,
    stop_event: asyncio.Event,
    activity: RtuActivity | None = None,
    client=None,
    mode: str = "run",
    clients: dict[str, object] | None = None,
    only: str | None = None,
) -> Pipeline:
    """Wire the enabled bridges: poller + output server + fail-safe for each.

    `only` restricts the process to a single bridge, which is how the deployment
    runs them: one systemd instance per bridge, so a crash, an OOM or a restart of
    one cannot take the other's RS-485 down with it. The whole config is still
    loaded and validated either way, which is what keeps the "two bridges must not
    share a serial port" check working across separately-run services.

    `activity` and `client` apply to the first selected bridge (the single-bridge
    callers and existing tests pass them); `clients` maps bridge name -> duck-typed
    client for the rest. A real AsyncModbusTcpClient needs a running loop, so tests
    inject fakes here rather than letting the factory run.
    """
    specs = select_specs(config, only)
    injected = dict(clients or {})
    if client is not None:
        injected.setdefault(specs[0].name, client)

    # Resolve and validate every source map before building anything: a bad
    # register_map on the last bridge would otherwise leave the earlier bridges'
    # coroutines created but never awaited.
    for spec in specs:
        side = registers.source_by_name(spec.source.register_map)
        if spec.source.type == "nd45":
            nd45_poller.validate_source_coverage(side)
        else:
            huawei_poller.validate_source_coverage(side)

    bridges: list[BridgeRuntime] = []
    coros: list = []
    for index, spec in enumerate(specs):
        bridge = build_bridge(
            spec,
            registers,
            activity=activity if index == 0 else None,
            client=injected.get(spec.name),
            # An explicit `activity` means a caller is watching reads (monitor,
            # rtudebug), so every bridge records -- otherwise the sibling bridges'
            # panels could not answer "is Sigenergy polling this port?". Not tied
            # to config.prometheus.enabled: see build_bridge's incident note.
            attended=activity is not None,
        )
        bridges.append(bridge)
        coros.extend(_bridge_coros(bridge, registers, config, stop_event))

    metrics_source = (
        MetricsSource(
            config=config,
            mode=mode,
            targets=registers.targets,
            watchdog_heartbeat=SlowestHeartbeat(b.heartbeat for b in bridges),
            bridges=[
                BridgeMetrics(
                    name=b.name,
                    spec=b.spec,
                    store=b.store,
                    activity=b.activity,
                    wire=b.wire,
                    poll_stats=b.poll_stats,
                    server_status=b.server_status,
                    recovery=b.recovery,
                    heartbeat=b.heartbeat,
                    client_ref=b,
                )
                for b in bridges
            ],
        )
        # Gated on prometheus.enabled alone, not on any bridge having activity:
        # metrics.py already renders activity/wire-dependent families as absent
        # when a bridge's activity/wire is None (2026-08-04: unattended `run`
        # never sets either -- see build_bridge). Canonical values, poll stats,
        # server status etc. do not depend on activity and should stay visible.
        if config.prometheus.enabled
        else None
    )
    return Pipeline(bridges=bridges, coros=coros, metrics=metrics_source)


def _bridge_coros(
    bridge: BridgeRuntime,
    registers: RegisterMap,
    config: AppConfig,
    stop_event: asyncio.Event,
) -> list:
    """The poller (with stall recovery) and the output supervisor for one bridge."""
    spec = bridge.spec
    targets = [side for _name, side in registers.targets]
    base_on_update = build_on_update(
        bridge.store, bridge.context, spec.dtsu.slave_id, targets,
        ct_ratio=spec.dtsu.identity.ir_at,
    )
    reporter = FaultReporter(label=f"bridge {spec.name!r}")

    def on_update(values: dict[str, float], ts: float) -> None:
        bridge.heartbeat.touch(time.monotonic())
        reporter.success()  # a good poll clears any active fault state
        bridge.spread.record(values)
        base_on_update(values, ts)
        # after base_on_update: a datastore write failure raises out of it and
        # lands in the poller's except -> on_error, so it must not count as OK
        bridge.poll_stats.record_ok(time.monotonic())

    def on_error(exc: Exception) -> None:
        bridge.heartbeat.touch(time.monotonic())
        reporter.failure(exc)
        bridge.poll_stats.record_error(exc, time.monotonic())

    poller = supervise_poller(
        bridge, registers, stop_event, on_update=on_update, on_error=on_error
    )
    supervisor = supervise_server(
        spec.dtsu, bridge.context, bridge.store, HealthGate(spec.safety.max_data_age_s),
        spec.safety.check_interval_s, stop_event,
        min_restart_interval=spec.safety.min_restart_interval_s,
        status=bridge.server_status,
        wire=bridge.wire,
    )
    return [poller, supervisor]


async def supervise_poller(
    bridge: BridgeRuntime,
    registers: RegisterMap,
    stop_event: asyncio.Event,
    on_update: Callable[[dict, float], None],
    on_error: Callable[[Exception], None],
    now: Callable[[], float] = time.monotonic,
    connect: Callable | None = None,
) -> None:
    """Run one bridge's poll loop, restarting it if it ever stops making progress.

    This is *not* the unreachable-source path: `run_poller` already survives that
    by cycling through its error handler, which keeps touching the heartbeat, so
    the store simply goes stale and `supervise_server` silences this bridge's
    output. What this adds is recovery from a genuinely *hung* await -- one that
    never returns -- which would otherwise leave the poll loop dead until a human
    noticed.

    That case used to be the systemd watchdog's job, but a process restart now
    takes every sibling bridge down with it, so it is handled in-process and per
    bridge instead. Throughout a recovery this bridge's output stays silenced
    (its data is stale by definition), so the fail-safe is never bypassed.
    """
    spec = bridge.spec
    poll_once_fn = _POLL_ONCE[spec.source.type]
    source_side = registers.source_by_name(spec.source.register_map)
    connect_fn = connect or connect_with_retry
    stall_timeout = spec.source.stall_timeout_s
    check_interval = min(spec.safety.check_interval_s, stall_timeout / 2)

    def start() -> asyncio.Task:
        bridge.heartbeat.touch(now())  # a fresh loop starts with a clean slate
        return asyncio.create_task(
            run_poller(
                bridge.client, source_side, spec.source.unit_id,
                spec.source.poll_interval_s, on_update, on_error, stop_event,
                poll_once_fn=poll_once_fn,
            )
        )

    task = start()
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
                break  # stop_event set
            except asyncio.TimeoutError:
                pass
            if task.done():
                # run_poller is written to return only when stop_event is set, so
                # reaching here means it died -- a bug that would otherwise leave
                # this bridge silently unpolled forever.
                log.error(
                    "bridge %r poll loop exited unexpectedly (%r); restarting",
                    bridge.name, task.exception(),
                )
            else:
                age = bridge.heartbeat.age(now())
                if age <= stall_timeout:
                    continue
                log.warning(
                    "bridge %r made no progress for %.1fs (> %.1fs); rebuilding its client",
                    bridge.name, age, stall_timeout,
                )
                await _cancel(task)
            bridge.recovery.record(now())
            _close_quietly(bridge.client, bridge.name)
            bridge.replace_client()
            await connect_fn(
                bridge.client, stop_event,
                spec.source.reconnect_delay_s, spec.source.reconnect_delay_max_s,
            )
            task = start()
    finally:
        await _cancel(task)


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - a dying poll loop must not stop recovery
        log.debug("poll loop raised while being cancelled", exc_info=True)


def _close_quietly(client, name: str) -> None:
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:  # noqa: BLE001 - a hung client may fail to close cleanly
        log.debug("bridge %r client close() failed", name, exc_info=True)


class SlowestHeartbeat:
    """Reports the age of whichever tracked poll loop is furthest behind.

    Feeds the systemd watchdog. With one bridge per systemd instance -- how the
    deployment runs -- this is simply that bridge's poller progress. When several
    bridges share a process (dev, `monitor`), any one of them stalling withholds
    the ping, which is the conservative choice: a restart is only correct if it
    fixes something, and a bridge that has stopped polling is not being served.
    """

    def __init__(self, beats) -> None:
        self._beats = list(beats)

    def age(self, now: float) -> float:
        return max((beat.age(now) for beat in self._beats), default=math.inf)


async def run_app(
    config: AppConfig,
    registers: RegisterMap,
    stop_event: asyncio.Event,
    client=None,
    clients: dict[str, object] | None = None,
    only: str | None = None,
) -> None:
    pipe = build_pipeline(
        config, registers, stop_event, client=client, clients=clients, only=only
    )
    notify_ready()

    # Started here (not inside build_pipeline/pipe.coros) so they run throughout
    # the initial connect below, not only after it succeeds -- otherwise a
    # prolonged source-unreachable-at-startup would starve the watchdog and
    # trigger a spurious restart, and a metrics endpoint that is down while a
    # source is unreachable is down exactly when it is needed.
    # The watchdog tracks poll-loop progress. supervise_poller gets first crack at
    # a stalled loop (source.stall_timeout_s is set below WatchdogSec), and systemd
    # restarts this instance only if that in-process recovery is itself looping.
    # Safe to escalate now that each bridge is its own systemd instance -- the
    # restart no longer takes a sibling bridge's RS-485 down with it.
    watchdog_sec = watchdog_seconds()
    extra_tasks: list[asyncio.Task] = []
    if watchdog_sec is not None:
        extra_tasks.append(
            asyncio.create_task(
                watchdog_loop(pipe.watchdog_heartbeat, watchdog_sec, stop_event)
            )
        )
    metrics_task = metrics.start(config, pipe.metrics, stop_event, only=only)

    # Bridges connect concurrently and independently: an unreachable source must
    # not delay bringing any sibling bridge up.
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
        await asyncio.gather(*pipe.coros, *extra_tasks)
    finally:
        for task in connects:
            task.cancel()
        for task in connects:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - must not mask the real exit reason
                log.debug("connect task failed during shutdown", exc_info=True)
        await metrics.stop(metrics_task)
        for bridge in pipe.bridges:
            _close_quietly(bridge.client, bridge.name)
