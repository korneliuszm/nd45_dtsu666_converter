"""Orchestration: wire poller + DTSU output server + fail-safe under one asyncio loop."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from pymodbus.client import AsyncModbusTcpClient

from .canonical import CanonicalStore, HealthGate
from .config import AppConfig, RegisterMap
from .dtsu_server import RtuActivity, build_context, supervise_server, update_datastore
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
    """Logs ND45 poll faults on state transitions, not once per failed poll.

    A sustained outage would otherwise emit a warning every poll interval
    (~200/min). This logs the first failure, a periodic summary, and recovery.
    """

    def __init__(self, logger=log, summary_interval: float = 60.0, clock=time.monotonic) -> None:
        self._log = logger
        self._summary_interval = summary_interval
        self._clock = clock
        self._failing = False
        self._count = 0
        self._last_summary = 0.0

    def failure(self, exc: Exception) -> None:
        now = self._clock()
        self._count += 1
        if not self._failing:
            self._failing = True
            self._last_summary = now
            self._log.warning("ND45 polling failed: %s (retrying; repeats muted)", exc)
        elif now - self._last_summary >= self._summary_interval:
            self._last_summary = now
            self._log.warning("ND45 still failing: %d polls failed so far", self._count)

    def success(self) -> None:
        if self._failing:
            self._log.info("ND45 polling recovered after %d failed poll(s)", self._count)
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
) -> Pipeline:
    """Wire poller + DTSU output server + fail-safe. Pass `activity` to record read requests."""
    validate_source_coverage(registers.nd45_source)
    store = CanonicalStore()
    gate = HealthGate(config.safety.max_data_age_s)
    targets = [
        registers.dtsu_target,
        registers.dtsu_sigen_ext_target,
        registers.dtsu_sigen_ext_energy,
    ]
    context = build_context(
        targets,
        config.dtsu.slave_id,
        activity=activity,
        dtsu_cfg=config.dtsu,
        sigen_identity=registers.dtsu_sigen_identity,
    )
    base_on_update = build_on_update(
        store, context, config.dtsu.slave_id, targets, ct_ratio=config.dtsu.identity.ir_at
    )
    reporter = FaultReporter()
    heartbeat = Heartbeat()

    def on_update(values: dict[str, float], ts: float) -> None:
        heartbeat.touch(time.monotonic())
        reporter.success()  # a good poll clears any active fault state
        base_on_update(values, ts)

    def on_error(exc: Exception) -> None:
        heartbeat.touch(time.monotonic())
        reporter.failure(exc)

    client = client or AsyncModbusTcpClient(
        config.nd45.host, port=config.nd45.port, timeout=config.nd45.timeout_s
    )
    poller = run_poller(
        client, registers.nd45_source, config.nd45.unit_id,
        config.nd45.poll_interval_s, on_update, on_error, stop_event,
    )
    supervisor = supervise_server(
        config.dtsu, context, store, gate,
        config.safety.check_interval_s, stop_event,
        min_restart_interval=config.safety.min_restart_interval_s,
    )
    return Pipeline(
        store=store, context=context, client=client, coros=[poller, supervisor], heartbeat=heartbeat
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
        pipe.client.close()
        return
    try:
        coros = [*pipe.coros, watchdog_task] if watchdog_task is not None else pipe.coros
        await asyncio.gather(*coros)
    finally:
        pipe.client.close()
