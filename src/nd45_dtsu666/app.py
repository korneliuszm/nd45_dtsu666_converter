"""Orchestration: wire poller + RTU server + fail-safe under one asyncio loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from pymodbus.client import AsyncModbusTcpClient

from .canonical import CanonicalStore, HealthGate
from .config import AppConfig, RegisterMap
from .dtsu_server import RtuActivity, build_context, supervise_server, update_datastore
from .nd45_poller import run_poller

log = logging.getLogger(__name__)


@dataclass
class Pipeline:
    """Assembled bridge components shared by `run` and `monitor`."""

    store: CanonicalStore
    context: object
    client: object
    coros: list


def build_on_update(store, context, slave_id, target) -> Callable[[dict, float], None]:
    def on_update(values: dict[str, float], ts: float) -> None:
        store.update(values, ts)
        update_datastore(context, slave_id, values, target)

    return on_update


def _on_error(exc: Exception) -> None:
    log.warning("poll failed: %s", exc)


async def connect_with_retry(
    client, stop_event: asyncio.Event, delay: float = 1.0, max_delay: float = 30.0
) -> bool:
    """Keep attempting the initial ND45 connect (backoff) until success or stop.

    pymodbus auto-reconnects a link that was up and dropped, but NOT an initial
    connect that never succeeded (e.g. the service starting before ND45 is
    reachable). This retry loop covers that startup race. Returns True once
    connected, or False if `stop_event` is set before any connection is made.
    """
    current = delay
    while not stop_event.is_set():
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
    """Wire poller + RTU server + fail-safe. Pass `activity` to record RTU reads."""
    store = CanonicalStore()
    gate = HealthGate(config.safety.max_data_age_s)
    context = build_context(registers.dtsu_target, config.dtsu.slave_id, activity=activity)
    on_update = build_on_update(store, context, config.dtsu.slave_id, registers.dtsu_target)

    client = client or AsyncModbusTcpClient(
        config.nd45.host, port=config.nd45.port, timeout=config.nd45.timeout_s
    )
    poller = run_poller(
        client, registers.nd45_source, config.nd45.unit_id,
        config.nd45.poll_interval_s, on_update, _on_error, stop_event,
    )
    supervisor = supervise_server(
        config.dtsu, context, store, gate,
        config.safety.check_interval_s, stop_event,
    )
    return Pipeline(store=store, context=context, client=client, coros=[poller, supervisor])


async def run_app(
    config: AppConfig,
    registers: RegisterMap,
    stop_event: asyncio.Event,
    client=None,
) -> None:
    pipe = build_pipeline(config, registers, stop_event, client=client)
    connected = await connect_with_retry(
        pipe.client, stop_event,
        config.nd45.reconnect_delay_s, config.nd45.reconnect_delay_max_s,
    )
    if not connected:  # stopped before we ever connected
        for coro in pipe.coros:
            coro.close()
        pipe.client.close()
        return
    try:
        await asyncio.gather(*pipe.coros)
    finally:
        pipe.client.close()
