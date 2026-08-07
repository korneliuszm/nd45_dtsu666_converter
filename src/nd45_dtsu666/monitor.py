"""Interactive commissioning dashboard, one screen per bridge.

Answers the two questions bring-up actually asks, per bridge and per RS-485 port:
is the **upstream link** up and delivering, and is **Sigenergy reading** on this
bridge's output? The served DTSU register dump is deliberately not here -- it is
long, it pushed the link and activity lines off screen, and `rtudebug` already
exists for tracing register blocks.

`render_bridge_monitor` is pure: it takes a `BridgeSnapshot` and returns text, so
it is testable without a running loop or a live bridge.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field

from . import metrics
from .app import build_pipeline, connect_with_retry
from .config import AppConfig, BridgeConf, RegisterMap
from .dtsu_server import RtuActivity

_SEP = "-" * 72


def _fmt(value: float | None, prec: int = 1) -> str:
    if value is None:
        return "-"
    if not math.isfinite(value):
        return "n/a"
    return f"{value:.{prec}f}"


def _age(value: float | None, suffix: str = "s ago") -> str:
    if value is None or not math.isfinite(value):
        return "never"
    return f"{value:.1f}{suffix}"


def _direction(p_total: float | None) -> str:
    if p_total is None:
        return "?"
    if p_total > 0:
        return "IMPORT (P>0)"
    if p_total < 0:
        return "EXPORT (P<0)"
    return "ZERO"


@dataclass
class BridgeSnapshot:
    """One bridge's state at a single instant. Plain data, no live references."""

    name: str
    source_kind: str  # "ND45" / "Huawei SmartLogger" / "STATIC DEBUG"
    source_desc: str  # host:port unit N
    output_desc: str  # /dev/ttyAMA3, or host:port
    slave_id: int
    canonical: dict[str, float] = field(default_factory=dict)
    data_age: float = math.inf
    max_data_age: float = 0.0
    poll_interval: float = 0.0
    # None when the client cannot report it (a test fake, or the static feeder).
    source_connected: bool | None = None
    polls_ok: int = 0
    polls_failed: int = 0
    consecutive_failures: int = 0
    last_ok_age: float = math.inf
    last_error_age: float = math.inf
    last_error_type: str | None = None
    poller_restarts: int = 0
    server_up: bool = False
    server_starts: int = 0
    server_stops: int = 0
    # SampleStats.summary() per point: the recent spread of the source's own
    # readings, which the instantaneous value on screen cannot show.
    spread: dict[str, dict | None] = field(default_factory=dict)
    # RtuActivity.summary() output for this bridge's output port, or None.
    activity: dict | None = None
    # WireActivity.summary(): raw RS-485 traffic, counted before pymodbus filters
    # by slave id. None for a TCP output or when nothing is recording.
    wire: dict | None = None

    @property
    def healthy(self) -> bool:
        return self.data_age <= self.max_data_age


def snapshot_bridge(bridge, loop_now: float, mono_now: float) -> BridgeSnapshot:
    """Build a snapshot from a live `app.BridgeRuntime`.

    Two clocks on purpose (CLAUDE.md): data age comes from the asyncio loop clock
    the poller stamps, activity ages from `time.monotonic()`.
    """
    spec = bridge.spec
    stats = bridge.poll_stats
    values, _ts = bridge.store.snapshot()
    return BridgeSnapshot(
        name=bridge.name,
        source_kind=source_kind(spec),
        source_desc=source_desc(spec),
        output_desc=output_desc(spec),
        slave_id=spec.dtsu.slave_id,
        canonical=values,
        data_age=bridge.store.age(loop_now),
        max_data_age=spec.safety.max_data_age_s,
        poll_interval=spec.source.poll_interval_s,
        source_connected=getattr(bridge.client, "connected", None),
        polls_ok=stats.polls_ok,
        polls_failed=stats.polls_failed,
        consecutive_failures=stats.consecutive_failures,
        last_ok_age=_since(stats.last_ok_ts, mono_now),
        last_error_age=_since(stats.last_error_ts, mono_now),
        last_error_type=stats.last_error_type,
        poller_restarts=bridge.recovery.restarts,
        server_up=bridge.server_status.running,
        server_starts=bridge.server_status.starts,
        server_stops=bridge.server_status.stops,
        spread={
            name: bridge.spread.summary(name)
            for name in getattr(bridge.spread, "_points", ())
        } if getattr(bridge, "spread", None) else {},
        activity=bridge.activity.summary(mono_now) if bridge.activity else None,
        wire=bridge.wire.summary(mono_now) if getattr(bridge, "wire", None) else None,
    )


def _since(ts: float | None, now: float) -> float:
    return math.inf if ts is None else now - ts


_SOURCE_KIND = {
    "nd45": "ND45",
    "huawei": "Huawei SmartLogger",
    "etango": "CHINT e2TANGO (4x)",
}


def source_kind(spec: BridgeConf) -> str:
    return _SOURCE_KIND[spec.source.type]


def source_desc(spec: BridgeConf) -> str:
    src = spec.source
    if src.type == "etango":
        return ", ".join(f"{d.host}:{d.port}/{d.unit_id}" for d in src.devices)
    return f"{src.host}:{src.port} unit {src.unit_id}"


def output_desc(spec: BridgeConf) -> str:
    dtsu = spec.dtsu
    if dtsu.transport == "rtu" and dtsu.rtu is not None:
        rtu = dtsu.rtu
        return f"{rtu.port} @{rtu.baudrate} 8{rtu.parity}{rtu.stopbits}"
    if dtsu.tcp is not None:
        return f"tcp {dtsu.tcp.host}:{dtsu.tcp.port}"
    return dtsu.transport


def render_bridge_monitor(snap: BridgeSnapshot) -> str:
    """Render one bridge: upstream link, decoded values, and Sigenergy activity."""
    state = "SERVING" if snap.healthy else "FAIL-SAFE SILENT"
    title = f" bridge {snap.name!r}   {snap.source_kind} -> DTSU666"
    # Pad to a column, but never let a long bridge name run into the state.
    pad = " " * max(3, len(_SEP) - 24 - len(title))
    lines = [
        f"{title}{pad}state: {state}",
        _SEP,
        *_render_source(snap),
        _SEP,
        *_render_output(snap),
        _SEP,
        " Ctrl-C to quit",
    ]
    return "\n".join(lines)


def _link_state(snap: BridgeSnapshot) -> str:
    if snap.source_connected is None:
        return "UNKNOWN"
    return "CONNECTED" if snap.source_connected else "DOWN"


def _render_source(snap: BridgeSnapshot) -> list[str]:
    poll = "OK" if snap.healthy else "STALE"
    age = "never polled" if not math.isfinite(snap.data_age) else f"{snap.data_age:.2f}s"
    lines = [
        f" SOURCE  {snap.source_kind}  {snap.source_desc}",
        f"   link: {_link_state(snap):<11}"
        f"data age: {age} / {snap.max_data_age:.1f}s   "
        f"poll: {poll} (every {snap.poll_interval:g}s)",
        f"   polls: {snap.polls_ok} ok / {snap.polls_failed} err"
        f"   consecutive fails: {snap.consecutive_failures}"
        f"   last ok: {_age(snap.last_ok_age)}",
    ]
    if snap.last_error_type is not None:
        lines.append(
            f"   last error: {snap.last_error_type} ({_age(snap.last_error_age)})"
            f"   poller restarts: {snap.poller_restarts}"
        )
    lines.append(
        f"   {'Phase':<7}{'U [V]':>9}{'I [A]':>9}{'P [W]':>12}{'Q [var]':>12}{'PF':>8}"
    )
    for phase, suf in (("L1", "l1"), ("L2", "l2"), ("L3", "l3")):
        lines.append(
            f"   {phase:<7}"
            f"{_fmt(snap.canonical.get(f'u_{suf}')):>9}"
            f"{_fmt(snap.canonical.get(f'i_{suf}'), 2):>9}"
            f"{_fmt(snap.canonical.get(f'p_{suf}')):>12}"
            f"{_fmt(snap.canonical.get(f'q_{suf}')):>12}"
            f"{_fmt(snap.canonical.get(f'pf_{suf}'), 3):>8}"
        )
    p_total = snap.canonical.get("p_total")
    lines.append(
        f"   {'TOTAL':<7}{'':>9}{'':>9}"
        f"{_fmt(p_total):>12}{_fmt(snap.canonical.get('q_total')):>12}"
        f"{_fmt(snap.canonical.get('pf_total'), 3):>8}"
        f"   f={_fmt(snap.canonical.get('freq'), 2)} Hz"
    )
    lines.extend(_render_spread(snap))
    lines.append(
        f"   Direction: {_direction(p_total)}      "
        f"E_imp={_fmt(snap.canonical.get('imp_energy_total'))}  "
        f"E_exp={_fmt(snap.canonical.get('exp_energy_total'))} kWh"
    )
    extra = [
        (label, snap.canonical.get(key))
        for label, key in (("E_daily", "e_daily"), ("P_dc", "dc_power"))
        if snap.canonical.get(key) is not None
    ]
    if extra:
        lines.append(
            "   " + "   ".join(f"{label}={_fmt(value)}" for label, value in extra)
        )
    return lines


def _render_spread(snap: BridgeSnapshot) -> list[str]:
    """How much the source's own reading moved over the last few polls.

    Without this, a value that swings faster than the eye can follow looks like a
    fault in the bridge. It is the same statistic `diag` prints, so the two can
    be compared -- and comparing them at different poll rates is exactly how a
    real swing gets mistaken for one.
    """
    stat = snap.spread.get("p_total")
    if stat is None:
        return []
    flips = stat["sign_changes"]
    line = (
        f"   P spread (last {stat['n']} polls): "
        f"{stat['min']:.0f} .. {stat['max']:.0f} W"
        f"   peak-peak {stat['max'] - stat['min']:.0f}"
    )
    if flips:
        line += f"   sign flips: {flips}"
    return [line]


def _render_output(snap: BridgeSnapshot) -> list[str]:
    server = "UP" if snap.server_up else "SILENT (fail-safe)"
    lines = [
        f" OUTPUT  DTSU666 slave {snap.slave_id}  on {snap.output_desc}",
        f"   server: {server:<20}starts: {snap.server_starts}  stops: {snap.server_stops}",
    ]
    if snap.activity is None:
        lines.append("   Sigenergy reads: (not recorded)")
        return lines
    act = snap.activity
    lines.append(
        f"   Sigenergy reads: {act['total']}    rate: {act['rate']:.1f}/s"
        f"    last seen: {_age(act['last_seen_age'])}"
    )
    if act["blocks"]:
        blocks = "   ".join(
            f"FC{fc:02d} @{addr} x{cnt} ({hits})"
            for (fc, addr, cnt), hits in act["blocks"][:5]
        )
    else:
        blocks = "(none yet -- Sigenergy has not polled this port)"
    lines.append(f"   blocks read:  {blocks}")
    if act["recent"]:
        recent = "  ".join(
            f"@{addr}x{cnt}" for (_ts, _fc, addr, cnt) in list(act["recent"])[-5:]
        )
        lines.append(f"   recent:  {recent}")
    lines.extend(_render_wire(snap))
    return lines


def _render_wire(snap: BridgeSnapshot) -> list[str]:
    """Raw bus traffic, which the read counter above cannot see.

    A request addressed to another slave id is dropped by pymodbus's RTU framer
    before it reaches the datastore, so "Sigenergy reads: 0" alone cannot
    distinguish a silent bus from a wrong address. This line does.
    """
    if snap.wire is None:
        return []
    wire = snap.wire
    if not wire["bytes_seen"]:
        return ["   bus traffic:  none -- nothing is transmitting on this port"]
    ids = wire["slave_ids"]
    line = (
        f"   bus traffic:  {wire['bytes_seen']} B"
        f"    last: {_age(wire['last_seen_age'])}"
    )
    if ids:
        addressed = "  ".join(f"slave {sid} x{hits}" for sid, hits in ids[:4])
        line += f"    frames for: {addressed}"
        foreign = [sid for sid, _hits in ids if sid != snap.slave_id]
        if foreign and snap.activity is not None and not snap.activity["total"]:
            line += f"  <- WRONG ADDRESS (this bridge answers slave {snap.slave_id})"
    else:
        line += "    no valid Modbus frame decoded (baudrate/framing/wiring?)"
    return [line]


def select_bridge_by_source(config: AppConfig, source_type: str) -> str:
    """Name of the first enabled bridge with this source type.

    Used by the per-source monitor commands so `monitor_hsm` still finds the
    SmartLogger bridge if it was given a different name in config.json.
    """
    for spec in config.bridge_specs:
        if spec.source.type == source_type:
            return spec.name
    configured = ", ".join(
        f"{s.name} ({s.source.type})" for s in config.bridge_specs
    )
    raise SystemExit(
        f"no enabled bridge with source type {source_type!r} (configured: {configured})"
    )


async def run_monitor(
    config: AppConfig,
    registers: RegisterMap,
    stop_event: asyncio.Event,
    refresh: float = 1.0,
    bridge_name: str | None = None,
) -> None:
    """Run every enabled bridge, displaying `bridge_name` (or all) every `refresh` s.

    Every bridge runs regardless of what is displayed -- the process behaves exactly
    as it does under `run`, so watching one bridge never changes the other's timing
    or leaves its output unserved.
    """
    # Check the name before building anything, or a typo leaks the pipeline's
    # already-created coroutines on the way out.
    if bridge_name is not None:
        known = [spec.name for spec in config.bridge_specs]
        if bridge_name not in known:
            raise KeyError(f"no bridge named {bridge_name!r} (have: {', '.join(known)})")

    activity = RtuActivity()
    pipe = build_pipeline(config, registers, stop_event, activity=activity, mode="monitor")
    shown = pipe.bridges if bridge_name is None else [pipe.bridge(bridge_name)]
    metrics_task = metrics.start(config, pipe.metrics, stop_event)

    # Bridges connect concurrently: an unreachable source must not hold up a
    # sibling bridge's dashboard or its output.
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

    async def _display() -> None:
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            loop_now, mono_now = loop.time(), time.monotonic()
            panels = [
                render_bridge_monitor(snapshot_bridge(bridge, loop_now, mono_now))
                for bridge in shown
            ]
            if len(shown) < len(pipe.bridges):
                others = ", ".join(
                    b.name for b in pipe.bridges if b not in shown
                )
                panels.append(f" also running (not shown): {others}")
            print("\033[2J\033[H", end="")  # clear screen
            print("\n".join(panels))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=refresh)
            except asyncio.TimeoutError:
                pass

    try:
        await asyncio.gather(*pipe.coros, _display())
    finally:
        for task in connects:
            task.cancel()
        for task in connects:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - shutdown must not mask the exit reason
                pass
        await metrics.stop(metrics_task)
        for bridge in pipe.bridges:
            close = getattr(bridge.client, "close", None)
            if close is not None:
                close()
