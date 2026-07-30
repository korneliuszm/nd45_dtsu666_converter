"""Interactive commissioning dashboard: live ND45 table + output read activity."""

from __future__ import annotations

import asyncio
import math
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


def _render_pv_panel(canonical, pv_age: float) -> list[str]:
    """PV production panel fed by the Huawei SmartLogger (telemetry only).

    Labelled "telemetry" on purpose: these values never gate the DTSU output, so
    a stale PV panel next to a healthy bridge is expected, not a fault.
    """
    stale = "" if math.isfinite(pv_age) else "   (never polled)"
    lines = [
        f" PV production (SmartLogger, telemetry)   data age: {_fmt(pv_age, 2)}s{stale}"
    ]
    p_total = canonical.get("pv_p_total")
    if p_total is None:
        lines.append("   (no data yet)")
        return lines
    lines.append(
        f"   P={_fmt(p_total):>12} W   Q={_fmt(canonical.get('pv_q_total')):>12} var"
        f"   PF={_fmt(canonical.get('pv_pf_total'), 3)}"
    )
    lines.append(
        f"   E_total={_fmt(canonical.get('pv_exp_energy_total'))} kWh"
        f"   E_daily={_fmt(canonical.get('pv_e_daily'))} kWh"
        f"   P_dc={_fmt(canonical.get('pv_dc_power'))} W"
    )
    lines.append(
        f"   I: {_fmt(canonical.get('pv_i_l1'), 1)} / {_fmt(canonical.get('pv_i_l2'), 1)}"
        f" / {_fmt(canonical.get('pv_i_l3'), 1)} A"
        f"   U_ll: {_fmt(canonical.get('pv_u_l12'))} / {_fmt(canonical.get('pv_u_l23'))}"
        f" / {_fmt(canonical.get('pv_u_l31'))} V"
    )
    return lines


def render_dashboard(
    canonical,
    age,
    healthy,
    activity,
    slave_id,
    now,
    source_label: str = "ND45",
    pv_age: float | None = None,
) -> str:
    """Render the dashboard (source values + Sigenergy read activity).

    A third PV panel appears when `pv_age` is given (huawei.enabled), showing the
    SmartLogger's production alongside the grid-tie measurement.
    """
    state = "SERVING" if healthy else "FAIL-SAFE SILENT"
    poll = "OK" if healthy else "STALE"
    lines = [
        f" {source_label} -> DTSU666  monitor                     state: {state}",
        _SEP,
        f" {source_label} (source)                    data age: {age:.2f}s   poll: {poll}",
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

    if pv_age is not None:
        lines.extend(_render_pv_panel(canonical, pv_age))
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
    lines.append(" Ctrl-C to quit")
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
    """Run the live bridge with a commissioning dashboard refreshed every `refresh` s."""
    activity = RtuActivity()
    pipe = build_pipeline(config, registers, stop_event, activity=activity, mode="monitor")
    metrics_task = metrics.start(config, pipe.metrics, stop_event)
    if not await connect_with_retry(
        pipe.client, stop_event,
        config.nd45.reconnect_delay_s, config.nd45.reconnect_delay_max_s,
    ):
        for coro in pipe.coros:
            coro.close()
        await metrics.stop(metrics_task)
        pipe.client.close()
        return

    # Same target set the served datastore was built from (app.build_pipeline).
    targets = [
        registers.dtsu_target,
        registers.dtsu_sigen_ext_target,
        registers.dtsu_sigen_ext_energy,
    ]

    # Read through the merged store when a secondary source is active, so the PV
    # panel and the register table see its points too. `age`/`healthy` still come
    # from the primary source alone -- MergedStore.age delegates to it.
    view = pipe.merged_store if pipe.merged_store is not None else pipe.store

    async def _display() -> None:
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            loop_now = loop.time()
            age = view.age(loop_now)
            healthy = age <= config.safety.max_data_age_s
            values, _ = view.snapshot()
            pv_age = (
                pipe.huawei_store.age(loop_now) if pipe.huawei_store is not None else None
            )
            now = time.monotonic()
            print("\033[2J\033[H", end="")  # clear screen
            print(
                render_dashboard(
                    values, age, healthy, activity, config.dtsu.slave_id, now,
                    pv_age=pv_age,
                )
            )
            print(
                render_registers_table(
                    targets, values, activity, now, ct_ratio=config.dtsu.identity.ir_at
                )
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=refresh)
            except asyncio.TimeoutError:
                pass

    try:
        await asyncio.gather(*pipe.coros, _display())
    finally:
        await metrics.stop(metrics_task)
        pipe.client.close()
