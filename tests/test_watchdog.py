import asyncio
import math
import socket

import pytest

import nd45_dtsu666.watchdog as watchdog
from nd45_dtsu666.watchdog import (
    Heartbeat,
    notify_ready,
    notify_watchdog,
    watchdog_loop,
    watchdog_seconds,
)


def test_heartbeat_starts_infinitely_stale():
    hb = Heartbeat()
    assert hb.age(now=100.0) == float("inf")


def test_heartbeat_touch_and_age():
    hb = Heartbeat()
    hb.touch(100.0)
    assert hb.age(now=103.5) == pytest.approx(3.5)


def _recv_datagram(sock, timeout=1.0):
    sock.settimeout(timeout)
    data, _ = sock.recvfrom(1024)
    return data


def test_notify_ready_sends_datagram(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(sock_path)
    monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

    notify_ready()

    assert _recv_datagram(server) == b"READY=1"
    server.close()


def test_notify_watchdog_sends_datagram(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(sock_path)
    monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

    notify_watchdog()

    assert _recv_datagram(server) == b"WATCHDOG=1"
    server.close()


def test_notify_is_noop_without_notify_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notify_ready()  # must not raise
    notify_watchdog()  # must not raise


def test_notify_swallows_oserror_from_dead_socket(tmp_path, monkeypatch):
    # NOTIFY_SOCKET set but nothing bound there (stale/removed socket):
    # a transient sd_notify failure must never crash the app it is
    # supposed to keep alive.
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "never-bound.sock"))
    notify_ready()  # must not raise
    notify_watchdog()  # must not raise


def test_watchdog_seconds_parses_watchdog_usec(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "90000000")  # 90s in microseconds
    assert watchdog_seconds() == pytest.approx(90.0)


def test_watchdog_seconds_none_when_unset(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert watchdog_seconds() is None


def test_watchdog_seconds_none_when_invalid(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "not-a-number")
    assert watchdog_seconds() is None


async def test_watchdog_loop_pings_while_heartbeat_fresh(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(sock_path)
    server.setblocking(False)
    monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

    hb = Heartbeat()
    clock = {"t": 100.0}
    hb.touch(clock["t"])
    stop = asyncio.Event()

    task = asyncio.create_task(
        watchdog_loop(hb, watchdog_sec=1.0, stop_event=stop, now=lambda: clock["t"])
    )
    await asyncio.sleep(0.05)  # first check runs immediately, before any wait
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    data, _ = server.recvfrom(1024)
    assert data == b"WATCHDOG=1"
    server.close()


async def test_watchdog_loop_withholds_ping_when_heartbeat_stale(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(sock_path)
    server.setblocking(False)
    monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

    hb = Heartbeat()
    clock = {"t": 100.0}
    hb.touch(0.0)  # 100s stale relative to now() below, watchdog_sec is only 1.0
    stop = asyncio.Event()

    task = asyncio.create_task(
        watchdog_loop(hb, watchdog_sec=1.0, stop_event=stop, now=lambda: clock["t"])
    )
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    with pytest.raises(BlockingIOError):
        server.recvfrom(1024)  # no datagram sent -- stale heartbeat withheld the ping
    server.close()


# --- what feeds the systemd watchdog ----------------------------------------


def test_slowest_heartbeat_reports_the_least_progressing_loop():
    """With one bridge per systemd instance this is just that bridge's poller."""
    from nd45_dtsu666.app import SlowestHeartbeat

    fast, slow = Heartbeat(), Heartbeat()
    fast.touch(99.0)
    slow.touch(50.0)
    assert SlowestHeartbeat([fast]).age(100.0) == pytest.approx(1.0)
    # a stalled sibling withholds the ping: a restart is only correct if it fixes
    # something, and a bridge that stopped polling is not being served
    assert SlowestHeartbeat([fast, slow]).age(100.0) == pytest.approx(50.0)


def test_slowest_heartbeat_is_infinite_before_any_poll():
    from nd45_dtsu666.app import SlowestHeartbeat

    assert SlowestHeartbeat([Heartbeat()]).age(100.0) == math.inf
    assert SlowestHeartbeat([]).age(100.0) == math.inf


async def test_watchdog_pings_while_the_poll_loop_progresses(monkeypatch):
    from nd45_dtsu666.app import SlowestHeartbeat

    sent = []
    monkeypatch.setattr(watchdog, "notify_watchdog", lambda: sent.append(1))
    beat = Heartbeat()
    beat.touch(1000.0)
    stop = asyncio.Event()
    task = asyncio.create_task(
        watchdog.watchdog_loop(
            SlowestHeartbeat([beat]), 0.04, stop, now=lambda: 1000.0
        )
    )
    await asyncio.sleep(0.06)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert sent, "the watchdog kept pinging while the poller made progress"


async def test_watchdog_withholds_the_ping_when_the_poll_loop_stalls(monkeypatch):
    """systemd restarts this instance -- safe now that each bridge is its own."""
    from nd45_dtsu666.app import SlowestHeartbeat

    sent = []
    monkeypatch.setattr(watchdog, "notify_watchdog", lambda: sent.append(1))
    beat = Heartbeat()
    beat.touch(0.0)  # last progress long ago
    stop = asyncio.Event()
    task = asyncio.create_task(
        watchdog.watchdog_loop(
            SlowestHeartbeat([beat]), 0.02, stop, now=lambda: 1000.0
        )
    )
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert sent == []


def test_stall_recovery_fires_before_the_watchdog_would():
    """Ordering that makes in-process recovery the first attempt, not a race.

    source.stall_timeout_s must stay below WatchdogSec, or systemd would restart
    the service before supervise_poller ever got to rebuild the client.
    """
    from nd45_dtsu666.config import load_config

    watchdog_sec = 90.0  # systemd/nd45-dtsu666@.service
    for spec in load_config("config/config.json").bridge_specs:
        assert spec.source.stall_timeout_s < watchdog_sec, spec.name
