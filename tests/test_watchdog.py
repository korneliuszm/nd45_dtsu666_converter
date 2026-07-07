import asyncio
import socket

import pytest

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
