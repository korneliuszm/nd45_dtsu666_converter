"""systemd watchdog (sd_notify) integration: liveness tracking + heartbeat loop."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import socket
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


class Heartbeat:
    """Tracks the last time a monitored loop (ND45 connect/poll) made progress."""

    def __init__(self) -> None:
        self._last: float | None = None

    def touch(self, ts: float) -> None:
        self._last = ts

    def age(self, now: float) -> float:
        return math.inf if self._last is None else now - self._last


def _notify(payload: str) -> None:
    """Send an sd_notify datagram. No-op if not run under systemd
    (NOTIFY_SOCKET unset), matching sd_notify(3)'s own contract."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]  # abstract namespace socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(payload.encode())
    except OSError:
        log.warning("sd_notify failed to send %r", payload, exc_info=True)


def notify_ready() -> None:
    """Tell systemd (Type=notify) that startup is complete."""
    _notify("READY=1")


def notify_watchdog() -> None:
    """Ping systemd's watchdog timer (WatchdogSec=)."""
    _notify("WATCHDOG=1")


def watchdog_seconds() -> float | None:
    """Read the watchdog interval systemd set via WATCHDOG_USEC, in seconds.
    Returns None if unset or invalid (watchdog not configured for this run)."""
    raw = os.environ.get("WATCHDOG_USEC")
    if not raw:
        return None
    try:
        usec = int(raw)
    except ValueError:
        return None
    if usec <= 0:
        return None
    return usec / 1_000_000


async def watchdog_loop(
    heartbeat: Heartbeat,
    watchdog_sec: float,
    stop_event: asyncio.Event,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Ping the systemd watchdog while `heartbeat` shows recent progress.

    Pings at watchdog_sec/2 (systemd's own recommendation). If the tracked
    loop has stalled longer than the full watchdog_sec, stops pinging so
    systemd's WatchdogSec fires and Restart=always restarts the service.
    """
    interval = watchdog_sec / 2
    while not stop_event.is_set():
        age = heartbeat.age(now())
        if age <= watchdog_sec:
            notify_watchdog()
        else:
            log.warning(
                "Watchdog heartbeat stale (age=%.1fs > %.1fs); withholding ping",
                age, watchdog_sec,
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
