"""Interactive commissioning dashboard: per-bridge source table + output read activity."""

from __future__ import annotations

import asyncio
import time

from . import metrics
from .app import build_pipeline, connect_with_retry
from .codec import registers_to_float
from .config import AppConfig, RegisterMap
from .dtsu_server import RtuActivity, encode_target_point

_SEP = "-" * 68
_READ_FRESH_S = 10.0  # a point is shown as "read" if seen within this many seconds


def _fmt(value: float | None, prec: int = 1) -> str:
    return f"{value:.{prec}f}" if value is not None else "-"


def _direction(p_total: float | None) -> str:
    if p_total is None:
        return "?"
    if p_total > 0:
        return "IMPORT (P>0)"
    if p_total < 0:
        return "EXPORT (P<0)"
    return "ZERO"


def render_dashboard(
    canonical,
    age,
    healthy,
    activity,
    slave_id,
    now,
    source_label: str = "ND45",
    output_label: str = "",
) -> str:
    """Render one bridge's panel pair: its source values + its Sigenergy activity.

    `output_label` names where this bridge serves (serial port or host:port), which
    is what tells two bridges apart at a glance when both are on screen.
    """
    state = "SERVING" if healthy else "FAIL-SAFE SILENT"
    poll = "OK" if healthy else "STALE"
    where = f"  [{output_label}]" if output_label else ""
    lines = [
        f" {source_label} -> DTSU666{where}                state: {state}",
        _SEP,
        f" source                                data age: {age:.2f}s   poll: {poll}",
        f"   {'Phase':<7}{'U [V]':>9}{'I [A]':>9}{'P [W]':>10}{'Q [var]':>10}{'PF':>8}",
    ]
    for phase, suf in (("L1", "l1"), ("L2", "l2"), ("L3", "l3")):
        lines.append(
            f"   {phase:<7}"
            f"{_fmt(canonical.get(f'u_{suf}')):>9}"
            f"{_fmt(canonical.get(f'i_{suf}'), 2):>9}"
            f"{_fmt(canonical.get(f'p_{suf}')):>10}"
            f"{_fmt(canonical.get(f'q_{suf}')):>10}"
            f"{_fmt(canonical.get(f'pf_{suf}'), 3):>8}"
        )
    p_total = canonical.get("p_total")
    lines.append(
        f"   {'TOTAL':<7}{'':>9}{'':>9}"
        f"{_fmt(p_total):>10}{_fmt(canonical.get('q_total')):>10}"
        f"{_fmt(canonical.get('pf_total'), 3):>8}   f={_fmt(canonical.get('freq'), 2)} Hz"
    )
    lines.append(
        f"   Direction: {_direction(p_total)}      "
        f"E_imp={_fmt(canonical.get('imp_energy_total'))}  "
        f"E_exp={_fmt(canonical.get('exp_energy_total'))} kWh"
    )
    lines.append(_SEP)

    s = activity.summary(now)
    last = "never" if s["last_seen_age"] is None else f"{s['last_seen_age']:.1f}s ago"
    lines.append(f" Sigenergy  (slave {slave_id})                     state: {state}")
    lines.append(f"   requests: {s['total']}    rate: {s['rate']:.1f}/s    last seen: {last}")
    if s["blocks"]:
        blocks = "   ".join(
            f"FC{fc:02d} @{addr} x{cnt} ({hits})" for (fc, addr, cnt), hits in s["blocks"][:6]
        )
    else:
        blocks = "(none yet)"
    lines.append(f"   blocks read:  {blocks}")
    if s["recent"]:
        recent = "  ".join(f"@{addr}x{cnt}" for (_ts, _fc, addr, cnt) in list(s["recent"])[-4:])
        lines.append(f"   recent:  {recent}")
    lines.append(_SEP)
    return "\n".join(lines)


def render_registers_table(
    targets,
    canonical: dict[str, float],
    activity: RtuActivity,
    now: float,
    ct_ratio: float = 1.0,
    read_window: float = _READ_FRESH_S,
) -> str:
    """Render every DTSU666 output register with its current value and a
    read-activity indicator ('*' if Sigenergy has read it within `read_window` s)."""
    lines = [
        f" DTSU666 output registers (served to Sigenergy)   "
        f"'*' = read in last {read_window:.0f}s",
        f"   {'point':<20}{'FC':>3}{'addr':>7}{'SI value':>14}{'reg raw':>14}   rd",
        "-" * 68,
    ]
    for side in targets:
        for pt in sorted(side.points.values(), key=lambda p: p.addr):
            si = canonical.get(pt.from_)
            if si is None:
                si_txt, raw_txt = "-", "-"
            else:
                regs = encode_target_point(si, pt, side, ct_ratio=ct_ratio)
                si_txt = f"{si:.3f}"
                raw_txt = f"{registers_to_float(regs, side.word_order, side.byte_order):.1f}"
            seen = activity.last_seen(side.function_code, pt.addr)
            read_mark = "*" if seen is not None and now - seen <= read_window else " "
            lines.append(
                f"   {pt.from_:<20}{side.function_code:>3}{pt.addr:>7}"
                f"{si_txt:>14}{raw_txt:>14}    {read_mark}"
            )
    lines.append(_SEP)
    return "\n".join(lines)


async def run_monitor(
    config: AppConfig, registers: RegisterMap, stop_event: asyncio.Event, refresh: float = 1.0
) -> None:
    """Run every enabled bridge with a commissioning dashboard, refreshed every `refresh` s.

    One panel pair per bridge (source values + that bridge's Sigenergy activity),
    plus the register table for each. Bridges are independent, so each shows its
    own SERVING / FAIL-SAFE SILENT state.
    """
    activity = RtuActivity()
    pipe = build_pipeline(config, registers, stop_event, activity=activity, mode="monitor")
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

    # Same target set the served datastore was built from (app.build_pipeline).
    targets = [side for _name, side in registers.targets]

    async def _display() -> None:
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            loop_now = loop.time()
            now = time.monotonic()
            panels = []
            for bridge in pipe.bridges:
                age = bridge.store.age(loop_now)
                healthy = age <= bridge.spec.safety.max_data_age_s
                values, _ = bridge.store.snapshot()
                bridge_activity = bridge.activity or activity
                panels.append(
                    render_dashboard(
                        values, age, healthy, bridge_activity,
                        bridge.spec.dtsu.slave_id, now,
                        source_label=_source_label(bridge),
                        output_label=_output_label(bridge),
                    )
                )
                panels.append(
                    render_registers_table(
                        targets, values, bridge_activity, now,
                        ct_ratio=bridge.spec.dtsu.identity.ir_at,
                    )
                )
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


def _source_label(bridge) -> str:
    kind = "ND45" if bridge.spec.source.type == "nd45" else "SmartLogger"
    return f"{bridge.name} ({kind})"


def _output_label(bridge) -> str:
    dtsu = bridge.spec.dtsu
    if dtsu.transport == "rtu" and dtsu.rtu is not None:
        return dtsu.rtu.port
    if dtsu.tcp is not None:
        return f"{dtsu.tcp.host}:{dtsu.tcp.port}"
    return dtsu.transport
