"""Serve stable, JSON-configured measurements without connecting to the ND45."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from . import metrics
from .canonical import CanonicalStore, HealthGate, compute_derived
from .config import AppConfig, RegisterMap
from .dtsu_server import RtuActivity, build_context, supervise_server, update_datastore
from .metrics import (
    BridgeMetrics,
    MetricsSource,
    PollStats,
    RecoveryStats,
    ServerStatus,
)
from .monitor import render_dashboard


@dataclass
class StaticPipeline:
    """Static source components; deliberately contains no ND45 client."""

    store: CanonicalStore
    context: object
    coros: list
    metrics: MetricsSource | None = None
    spec: object | None = None  # the BridgeConf whose output is being served


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
    # Static mode has no ND45 source registers. Derive only omitted apparent
    # power values as a convenience; explicit test values must remain exact.
    for suffix in ("l1", "l2", "l3"):
        s_key = f"s_{suffix}"
        if s_key not in configured:
            values[s_key] = abs(
                values.get(f"u_{suffix}", 0.0)
                * values.get(f"i_{suffix}", 0.0)
            )
    if "s_total" not in configured:
        values["s_total"] = sum(values[f"s_{suffix}"] for suffix in ("l1", "l2", "l3"))

    # Net energy remains common to static and live pipelines.
    compute_derived(values)
    return values


def build_static_pipeline(
    config: AppConfig,
    registers: RegisterMap,
    stop_event: asyncio.Event,
    activity: RtuActivity,
    bridge_name: str | None = None,
) -> StaticPipeline:
    """Build a static feeder plus the normal output server supervisor.

    Serves one bridge's output transport (`bridge_name`, default the first), so
    the synthetic image goes out on exactly the bus being bench-tested.
    """
    from .diagnostics import select_bridge

    spec = select_bridge(config, bridge_name)
    store = CanonicalStore()
    gate = HealthGate(spec.safety.max_data_age_s)
    named_targets = registers.targets
    targets = [side for _name, side in named_targets]
    server_status = ServerStatus()
    context = build_context(
        targets,
        spec.dtsu.slave_id,
        activity=activity,
        dtsu_cfg=spec.dtsu,
        sigen_identity=registers.dtsu_sigen_identity,
    )
    values = expand_static_values(registers, config.static_debug.values)
    # Encode once before creating any coroutines. This both seeds the context
    # and makes an invalid scaled/debug value fail synchronously at startup.
    update_datastore(
        context,
        spec.dtsu.slave_id,
        values,
        targets,
        ct_ratio=spec.dtsu.identity.ir_at,
    )

    async def feeder() -> None:
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            store.update(values, loop.time())
            update_datastore(
                context, spec.dtsu.slave_id, values, targets,
                ct_ratio=spec.dtsu.identity.ir_at,
            )
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=config.static_debug.feed_interval_s,
                )
            except asyncio.TimeoutError:
                pass

    supervisor = supervise_server(
        spec.dtsu,
        context,
        store,
        gate,
        spec.safety.check_interval_s,
        stop_event,
        min_restart_interval=spec.safety.min_restart_interval_s,
        status=server_status,
    )
    # No upstream client and no poller here, so the poll counters stay at zero and
    # the connected gauge is omitted entirely -- build_info's mode="static" is what
    # tells a scraper the served values are synthetic.
    source = MetricsSource(
        config=config,
        bridges=[
            BridgeMetrics(
                name=spec.name,
                spec=spec,
                store=store,
                poll_stats=PollStats(),
                server_status=server_status,
                recovery=RecoveryStats(),
                activity=activity,
            )
        ],
        targets=named_targets,
        mode="static",
    )
    return StaticPipeline(
        store=store, context=context, coros=[feeder(), supervisor], metrics=source, spec=spec
    )


async def run_static_debug(
    config: AppConfig,
    registers: RegisterMap,
    stop_event: asyncio.Event,
    refresh: float = 1.0,
    bridge_name: str | None = None,
) -> None:
    """Serve static data and show Sigenergy request activity until stopped."""
    activity = RtuActivity()
    pipe = build_static_pipeline(config, registers, stop_event, activity, bridge_name)
    metrics_task = metrics.start(config, pipe.metrics, stop_event)

    async def display() -> None:
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            age = pipe.store.age(loop.time())
            healthy = age <= pipe.spec.safety.max_data_age_s
            values, _ = pipe.store.snapshot()
            print("\033[2J\033[H", end="")
            print(
                render_dashboard(
                    values,
                    age,
                    healthy,
                    activity,
                    pipe.spec.dtsu.slave_id,
                    time.monotonic(),
                    source_label=f"STATIC DEBUG ({pipe.spec.name})",
                )
            )
            print(" Synthetic values from static_debug.values; no source is connected.")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=refresh)
            except asyncio.TimeoutError:
                pass

    try:
        await asyncio.gather(*pipe.coros, display())
    finally:
        await metrics.stop(metrics_task)
