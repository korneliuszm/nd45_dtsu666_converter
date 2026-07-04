"""Interactive commissioning dashboard: live ND45 table + RTU request activity."""

from __future__ import annotations

import asyncio
import time

from .app import build_pipeline
from .config import AppConfig, RegisterMap
from .dtsu_server import RtuActivity

_SEP = "-" * 68


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


def render_dashboard(canonical, age, healthy, activity, slave_id, now) -> str:
    """Render the two-panel dashboard (ND45 values + Sigenergy RTU activity)."""
    state = "SERVING" if healthy else "FAIL-SAFE SILENT"
    poll = "OK" if healthy else "STALE"
    lines = [
        f" ND45 -> DTSU666  monitor                     state: {state}",
        _SEP,
        f" ND45 (source)                    data age: {age:.2f}s   poll: {poll}",
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
    lines.append(f" Sigenergy RTU  (slave {slave_id})                 state: {state}")
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


async def run_monitor(
    config: AppConfig, registers: RegisterMap, stop_event: asyncio.Event, refresh: float = 1.0
) -> None:
    """Run the live bridge with a commissioning dashboard refreshed every `refresh` s."""
    activity = RtuActivity()
    pipe = build_pipeline(config, registers, stop_event, activity=activity)
    await pipe.client.connect()

    async def _display() -> None:
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            age = pipe.store.age(loop.time())
            healthy = age <= config.safety.max_data_age_s
            values, _ = pipe.store.snapshot()
            print("\033[2J\033[H", end="")  # clear screen
            print(
                render_dashboard(
                    values, age, healthy, activity, config.dtsu.slave_id, time.monotonic()
                )
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=refresh)
            except asyncio.TimeoutError:
                pass

    try:
        await asyncio.gather(*pipe.coros, _display())
    finally:
        pipe.client.close()
