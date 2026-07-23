"""Serve stable, JSON-configured measurements without connecting to the ND45."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .canonical import CanonicalStore, HealthGate
from .config import AppConfig, RegisterMap
from .dtsu_server import RtuActivity, build_context, supervise_server, update_datastore
from .monitor import render_dashboard


@dataclass
class StaticPipeline:
    """Static source components; deliberately contains no ND45 client."""

    store: CanonicalStore
    context: object
    coros: list


def expand_static_values(
    registers: RegisterMap, configured: dict[str, float]
) -> dict[str, float]:
    """Return every canonical value consumed by an output map, zero-filling omissions."""
    targets = (
        registers.dtsu_target,
        registers.dtsu_sigen_ext_target,
        registers.dtsu_sigen_ext_energy,
    )
    required = {
        point.from_
        for target in targets
        for point in target.points.values()
    }
    values = {name: configured.get(name, 0.0) for name in required}
    # derive net energy + apparent power from the configured base values so the
    # static feed matches what the live poller would produce (S = |U*I|, etc.)
    from .nd45_poller import compute_derived

    compute_derived(values)
    return values


def build_static_pipeline(
    config: AppConfig,
    registers: RegisterMap,
    stop_event: asyncio.Event,
    activity: RtuActivity,
) -> StaticPipeline:
    """Build a static feeder plus the normal output server supervisor."""
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
    values = expand_static_values(registers, config.static_debug.values)

    async def feeder() -> None:
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            store.update(values, loop.time())
            update_datastore(
                context, config.dtsu.slave_id, values, targets,
                ct_ratio=config.dtsu.identity.ir_at,
            )
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=config.static_debug.feed_interval_s,
                )
            except asyncio.TimeoutError:
                pass

    supervisor = supervise_server(
        config.dtsu,
        context,
        store,
        gate,
        config.safety.check_interval_s,
        stop_event,
        min_restart_interval=config.safety.min_restart_interval_s,
    )
    return StaticPipeline(store=store, context=context, coros=[feeder(), supervisor])


async def run_static_debug(
    config: AppConfig,
    registers: RegisterMap,
    stop_event: asyncio.Event,
    refresh: float = 1.0,
) -> None:
    """Serve static data and show Sigenergy request activity until stopped."""
    activity = RtuActivity()
    pipe = build_static_pipeline(config, registers, stop_event, activity)

    async def display() -> None:
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            age = pipe.store.age(loop.time())
            healthy = age <= config.safety.max_data_age_s
            values, _ = pipe.store.snapshot()
            print("\033[2J\033[H", end="")
            print(
                render_dashboard(
                    values,
                    age,
                    healthy,
                    activity,
                    config.dtsu.slave_id,
                    time.monotonic(),
                    source_label="STATIC DEBUG",
                )
            )
            print(" Synthetic values from static_debug.values; ND45 is not connected.")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=refresh)
            except asyncio.TimeoutError:
                pass

    await asyncio.gather(*pipe.coros, display())
