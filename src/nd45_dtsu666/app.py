"""Orchestration: wire poller + DTSU output server + fail-safe under one asyncio loop."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from pymodbus.client import AsyncModbusTcpClient

from . import huawei_poller, metrics
from .canonical import CanonicalStore, HealthGate, MergedStore
from .config import AppConfig, RegisterMap
from .dtsu_server import RtuActivity, build_context, supervise_server, update_datastore
from .metrics import MetricsSource, PollStats, ServerStatus
from .nd45_poller import run_poller, validate_source_coverage
from .watchdog import Heartbeat, notify_ready, watchdog_loop, watchdog_seconds

log = logging.getLogger(__name__)


@dataclass
class Pipeline:
    """Assembled bridge components shared by `run` and `monitor`."""

    store: CanonicalStore
    context: object
    client: object
    coros: list
    heartbeat: Heartbeat
    metrics: MetricsSource | None = None
    # Present only when huawei.enabled; `store` above stays the primary (ND45)
    # store so the freshness fail-safe is unaffected either way.
    merged_store: object | None = None
    huawei_store: CanonicalStore | None = None
    huawei_client: object | None = None


def build_on_update(
    store, context, slave_id, target, ct_ratio: float = 1.0, beneath=(), above=()
) -> Callable[[dict, float], None]:
    """Build the poller callback that mirrors canonical values into the datastore.

    With more than one source, every poll writes the *union* of all sources, so
    whichever poller fires still refreshes its own points without blanking the
    others. `beneath` holds stores this poller's values override; `above` holds
    stores that override this poller's values. The primary (ND45) source is
    always `above` a secondary, so a stray secondary point name can never
    displace a grid-tie measurement Sigenergy regulates on.
    """

    def on_update(values: dict[str, float], ts: float) -> None:
        if beneath or above:
            merged: dict[str, float] = {}
            for other in beneath:
                merged.update(other.snapshot()[0])
            merged.update(values)
            for other in above:
                merged.update(other.snapshot()[0])
        else:
            merged = values
        # Encode into the served datastore BEFORE stamping the store fresh: if
        # a write ever fails, the store stays stale so the freshness fail-safe
        # silences the output, instead of serving a half-written datastore to
        # Sigenergy as if it were fresh.
        update_datastore(context, slave_id, merged, target, ct_ratio=ct_ratio)
        store.update(values, ts)

    return on_update


class FaultReporter:
    """Logs poll faults on state transitions, not once per failed poll.

    A sustained outage would otherwise emit a warning every poll interval
    (~200/min). This logs the first failure, a periodic summary, and recovery.

    `label` names the source in the log line, so a second source's outage is not
    misreported as an ND45 fault while triaging from the journal.
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
    """Keep attempting the initial ND45 connect (backoff) until success or stop.

    pymodbus auto-reconnects a link that was up and dropped, but NOT an initial
    connect that never succeeded (e.g. the service starting before ND45 is
    reachable). This retry loop covers that startup race. Returns True once
    connected, or False if `stop_event` is set before any connection is made.
    """
    current = delay
    while not stop_event.is_set():
        if heartbeat is not None:
            heartbeat.touch(time.monotonic())
        if await client.connect():
            return True
        log.warning("ND45 not reachable; retrying connect in %.1fs", current)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=current)
        except asyncio.TimeoutError:
            pass
        current = min(current * 2, max_delay)
    return False


def build_pipeline(
    config: AppConfig,
    registers: RegisterMap,
    stop_event: asyncio.Event,
    activity: RtuActivity | None = None,
    client=None,
    mode: str = "run",
    huawei_client=None,
) -> Pipeline:
    """Wire poller + DTSU output server + fail-safe. Pass `activity` to record read requests.

    `client` / `huawei_client` override the constructed pymodbus clients (tests
    inject duck-typed fakes; a real AsyncModbusTcpClient needs a running loop).
    """
    validate_source_coverage(registers.nd45_source)
    store = CanonicalStore()
    huawei_sources = (
        huawei_poller.select_sources(config, registers) if config.huawei.enabled else []
    )
    huawei_store = CanonicalStore() if huawei_sources else None
    merged_store = (
        MergedStore(store, {"huawei": huawei_store}) if huawei_store is not None else None
    )
    # The freshness gate and every read site follow the primary source only; a
    # slow or absent SmartLogger must not be able to silence a healthy bridge.
    gate = HealthGate(config.safety.max_data_age_s)
    named_targets = [
        ("dtsu_target", registers.dtsu_target),
        ("dtsu_sigen_ext_target", registers.dtsu_sigen_ext_target),
        ("dtsu_sigen_ext_energy", registers.dtsu_sigen_ext_energy),
    ]
    targets = [side for _name, side in named_targets]
    # The metrics endpoint reports what Sigenergy actually reads, which only the
    # recording context can see -- so `run` needs an RtuActivity too, not just
    # the debug modes. An explicit `activity=` (monitor/rtudebug) still wins.
    if activity is None and config.prometheus.enabled:
        activity = RtuActivity()
    context = build_context(
        targets,
        config.dtsu.slave_id,
        activity=activity,
        dtsu_cfg=config.dtsu,
        sigen_identity=registers.dtsu_sigen_identity,
    )
    base_on_update = build_on_update(
        store, context, config.dtsu.slave_id, targets, ct_ratio=config.dtsu.identity.ir_at,
        beneath=(huawei_store,) if huawei_store is not None else (),
    )
    reporter = FaultReporter()
    heartbeat = Heartbeat()
    poll_stats = PollStats()
    server_status = ServerStatus()

    def on_update(values: dict[str, float], ts: float) -> None:
        heartbeat.touch(time.monotonic())
        reporter.success()  # a good poll clears any active fault state
        base_on_update(values, ts)
        # after base_on_update: a datastore write failure raises out of it and
        # lands in the poller's except -> on_error, so it must not count as OK
        poll_stats.record_ok(time.monotonic())

    def on_error(exc: Exception) -> None:
        heartbeat.touch(time.monotonic())
        reporter.failure(exc)
        poll_stats.record_error(exc, time.monotonic())

    client = client or AsyncModbusTcpClient(
        config.nd45.host, port=config.nd45.port, timeout=config.nd45.timeout_s
    )
    poller = run_poller(
        client, registers.nd45_source, config.nd45.unit_id,
        config.nd45.poll_interval_s, on_update, on_error, stop_event,
    )
    coros = [poller]
    huawei_poll_stats = None
    if huawei_store is None:
        huawei_client = None
    else:
        huawei_client = huawei_client or AsyncModbusTcpClient(
            config.huawei.host, port=config.huawei.port, timeout=config.huawei.timeout_s
        )
        # 5-minute summaries, not 1-minute: this source polls ~16x slower than
        # the ND45, and its outages are not service-affecting.
        huawei_reporter = FaultReporter(summary_interval=300.0, label="SmartLogger")
        huawei_poll_stats = PollStats()
        huawei_on_update_base = build_on_update(
            huawei_store, context, config.dtsu.slave_id, targets,
            ct_ratio=config.dtsu.identity.ir_at, above=(store,),
        )

        def huawei_on_update(values: dict[str, float], ts: float) -> None:
            # Deliberately does NOT touch `heartbeat`. The watchdog exists to
            # catch a hung ND45 poller; a live SmartLogger poller pinging it
            # would mask exactly that and stop systemd from ever restarting us.
            huawei_reporter.success()
            huawei_on_update_base(values, ts)
            huawei_poll_stats.record_ok(time.monotonic())

        def huawei_on_error(exc: Exception) -> None:
            huawei_reporter.failure(exc)
            huawei_poll_stats.record_error(exc, time.monotonic())

        coros.append(
            run_poller(
                huawei_client, huawei_sources, config.huawei.plant_unit_id or 0,
                config.huawei.poll_interval_s, huawei_on_update, huawei_on_error,
                stop_event, poll_once_fn=huawei_poller.poll_once,
            )
        )
    supervisor = supervise_server(
        config.dtsu, context, store, gate,
        config.safety.check_interval_s, stop_event,
        min_restart_interval=config.safety.min_restart_interval_s,
        status=server_status,
    )
    metrics_source = (
        MetricsSource(
            config=config,
            # The exporter reads the union so PV points show up as registers,
            # while `age`/freshness still track the primary source alone.
            store=merged_store if merged_store is not None else store,
            activity=activity,
            poll_stats=poll_stats,
            server_status=server_status,
            targets=named_targets,
            heartbeat=heartbeat,
            client=client,
            mode=mode,
            huawei_store=huawei_store,
            huawei_poll_stats=huawei_poll_stats,
            huawei_client=huawei_client,
        )
        if activity is not None
        else None
    )
    return Pipeline(
        store=store, context=context, client=client, coros=[*coros, supervisor],
        heartbeat=heartbeat, metrics=metrics_source,
        merged_store=merged_store, huawei_store=huawei_store, huawei_client=huawei_client,
    )


async def run_app(
    config: AppConfig,
    registers: RegisterMap,
    stop_event: asyncio.Event,
    client=None,
) -> None:
    pipe = build_pipeline(config, registers, stop_event, client=client)
    notify_ready()

    # Started here (not inside build_pipeline/pipe.coros) so it pings
    # throughout connect_with_retry's retry loop below, not only after it
    # succeeds -- otherwise a prolonged ND45-unreachable-at-startup would
    # starve the watchdog and trigger a spurious restart.
    watchdog_sec = watchdog_seconds()
    watchdog_task = (
        asyncio.create_task(watchdog_loop(pipe.heartbeat, watchdog_sec, stop_event))
        if watchdog_sec is not None
        else None
    )
    # Same reason: pipe.coros only start once connected, and a metrics endpoint
    # that is down while ND45 is unreachable is down exactly when it is needed.
    metrics_task = metrics.start(config, pipe.metrics, stop_event)

    # The SmartLogger connect runs as its own task, never awaited inline: it is
    # a secondary telemetry source, and an unreachable one must not delay (or
    # block) bringing the DTSU output up from the ND45.
    huawei_connect_task = (
        asyncio.create_task(
            connect_with_retry(
                pipe.huawei_client, stop_event,
                config.huawei.reconnect_delay_s, config.huawei.reconnect_delay_max_s,
            )
        )
        if pipe.huawei_client is not None
        else None
    )

    async def _shutdown_huawei() -> None:
        if huawei_connect_task is not None:
            huawei_connect_task.cancel()
            try:
                await huawei_connect_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - must not mask the real exit reason
                log.debug("SmartLogger connect failed during shutdown", exc_info=True)
        if pipe.huawei_client is not None:
            pipe.huawei_client.close()

    connected = await connect_with_retry(
        pipe.client, stop_event,
        config.nd45.reconnect_delay_s, config.nd45.reconnect_delay_max_s,
        heartbeat=pipe.heartbeat,
    )
    if not connected:  # stopped before we ever connected
        for coro in pipe.coros:
            coro.close()
        if watchdog_task is not None:
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass
        await _shutdown_huawei()
        await metrics.stop(metrics_task)
        pipe.client.close()
        return
    try:
        coros = [*pipe.coros, watchdog_task] if watchdog_task is not None else pipe.coros
        await asyncio.gather(*coros)
    finally:
        await _shutdown_huawei()
        await metrics.stop(metrics_task)
        pipe.client.close()
