# systemd Watchdog Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect a genuine hang of the ND45 poller (not just a process crash) and let systemd restart the bridge automatically, using systemd's `sd_notify` watchdog protocol.

**Architecture:** A new `watchdog.py` module implements the `sd_notify` wire protocol (a datagram to `$NOTIFY_SOCKET`) plus a `Heartbeat` that tracks the last time the ND45 poller made progress. `app.py` wires the heartbeat into `connect_with_retry` and `build_pipeline`'s poll success/failure callbacks, and conditionally adds a `watchdog_loop` coroutine that pings systemd only while the heartbeat is fresh. The systemd unit adds `Type=notify` + `WatchdogSec=90`; `Restart=always` (unchanged) handles the resulting restart exactly like it handles a crash today.

**Tech Stack:** Python 3.10+ stdlib only (`socket`, `os`, `asyncio`) — no new PyPI dependency. pytest/pytest-asyncio for tests (real local `AF_UNIX` sockets, no mocks).

## Global Constraints

- No new PyPI dependency — `sd_notify` is a hand-rolled datagram protocol (see Task 1), not the `sdnotify` package.
- No new fields in `config/config.json` — the watchdog interval comes from `WATCHDOG_USEC`, an environment variable systemd itself sets when `WatchdogSec=` is present in the unit; this avoids duplicating the interval between the service file and app config.
- Heartbeat liveness means **ND45 poller progress** (a connect attempt, a successful poll, or a handled poll failure) — never bare "the event loop is still scheduling tasks." A real ND45 network outage must NOT trigger a restart; only a genuine stuck poller may.
- `WatchdogSec=90` in the unit file (rationale: worst-case legitimate gap between heartbeat touches during ND45 reconnect backoff is `reconnect_delay_max_s` (30s, default) + `timeout_s` (1s, default) ≈ 31s; 90s gives ~3x margin).
- Line length 100 (`ruff` config in `pyproject.toml`); run `python -m ruff check .` before each commit that touches `.py` files.
- Test commands use the project venv: `.venv/bin/python -m pytest` / `.venv/bin/python -m ruff check .` (Linux); substitute `.venv\Scripts\python.exe` on Windows per `CLAUDE.md`.

---

### Task 1: `watchdog.py` — sd_notify protocol + heartbeat tracking

**Files:**
- Create: `src/nd45_dtsu666/watchdog.py`
- Test: `tests/test_watchdog.py`

**Interfaces:**
- Produces: `Heartbeat` — `touch(ts: float) -> None`, `age(now: float) -> float` (returns `math.inf` if never touched).
- Produces: `notify_ready() -> None` — sends `"READY=1"`.
- Produces: `notify_watchdog() -> None` — sends `"WATCHDOG=1"`.
- Produces: `watchdog_seconds() -> float | None` — parses `WATCHDOG_USEC` env var (microseconds) into seconds; `None` if unset/invalid.
- Produces: `async def watchdog_loop(heartbeat: Heartbeat, watchdog_sec: float, stop_event: asyncio.Event, now: Callable[[], float] = time.monotonic) -> None`.
- Consumes: nothing from other modules — this file has zero project-internal imports.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_watchdog.py`:

```python
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
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_watchdog.py -v`
Expected: `ModuleNotFoundError: No module named 'nd45_dtsu666.watchdog'` (the module doesn't exist yet).

- [ ] **Step 3: Implement `src/nd45_dtsu666/watchdog.py`**

```python
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
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `.venv/bin/python -m pytest tests/test_watchdog.py -v`
Expected: all 11 tests PASS.

- [ ] **Step 5: Lint**

Run: `.venv/bin/python -m ruff check .`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/nd45_dtsu666/watchdog.py tests/test_watchdog.py
git commit -m "feat: add systemd sd_notify watchdog protocol + heartbeat tracking"
```

---

### Task 2: Wire the heartbeat into `app.py`

**Files:**
- Modify: `src/nd45_dtsu666/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `Heartbeat`, `notify_ready`, `watchdog_loop`, `watchdog_seconds` from `src/nd45_dtsu666/watchdog.py` (Task 1).
- Produces: `Pipeline.heartbeat: Heartbeat` (new field) — later tasks/operators can inspect it, but nothing else in this codebase consumes it directly.
- Modifies (signature change, backward compatible via default): `connect_with_retry(client, stop_event, delay=1.0, max_delay=30.0, heartbeat: Heartbeat | None = None) -> bool`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_app.py`. First, change the top-level import line (currently):

```python
from nd45_dtsu666.app import FaultReporter, build_on_update, build_pipeline, connect_with_retry
from nd45_dtsu666.canonical import CanonicalStore
from nd45_dtsu666.codec import registers_to_float
from nd45_dtsu666.config import load_config, load_registers
from nd45_dtsu666.dtsu_server import RecordingSlaveContext, RtuActivity, build_context
```

to (adding the `time` import and the `Heartbeat` import):

```python
import time

from nd45_dtsu666.app import FaultReporter, build_on_update, build_pipeline, connect_with_retry
from nd45_dtsu666.canonical import CanonicalStore
from nd45_dtsu666.codec import registers_to_float
from nd45_dtsu666.config import load_config, load_registers
from nd45_dtsu666.dtsu_server import RecordingSlaveContext, RtuActivity, build_context
from nd45_dtsu666.watchdog import Heartbeat
```

(`import asyncio` is already the first line of the file — keep it there, just add `import time` right after it, before the blank line and the `nd45_dtsu666...` imports.)

Then append these new tests, and replace the two existing `test_build_pipeline_*` tests as shown (they need `monkeypatch` to guarantee `WATCHDOG_USEC` is unset in the environment the suite runs in, so their coros-count assertion is deterministic regardless of the host environment):

Replace:

```python
def test_build_pipeline_wires_components_and_threads_activity():
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    stop = asyncio.Event()
    activity = RtuActivity()

    pipe = build_pipeline(config, registers, stop, activity=activity, client=object())

    assert pipe.store is not None
    assert len(pipe.coros) == 2  # poller + supervisor
    # activity was threaded into a recording datastore context
    assert isinstance(pipe.context[config.dtsu.slave_id], RecordingSlaveContext)

    for coro in pipe.coros:  # never awaited in this test; close to keep output pristine
        coro.close()


def test_build_pipeline_default_context_not_recording():
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    pipe = build_pipeline(config, registers, asyncio.Event(), client=object())
    assert not isinstance(pipe.context[config.dtsu.slave_id], RecordingSlaveContext)
    for coro in pipe.coros:
        coro.close()
```

with:

```python
def test_build_pipeline_wires_components_and_threads_activity(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    stop = asyncio.Event()
    activity = RtuActivity()

    pipe = build_pipeline(config, registers, stop, activity=activity, client=object())

    assert pipe.store is not None
    assert len(pipe.coros) == 2  # poller + supervisor
    # activity was threaded into a recording datastore context
    assert isinstance(pipe.context[config.dtsu.slave_id], RecordingSlaveContext)

    for coro in pipe.coros:  # never awaited in this test; close to keep output pristine
        coro.close()


def test_build_pipeline_default_context_not_recording(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    pipe = build_pipeline(config, registers, asyncio.Event(), client=object())
    assert not isinstance(pipe.context[config.dtsu.slave_id], RecordingSlaveContext)
    for coro in pipe.coros:
        coro.close()


def test_build_pipeline_adds_watchdog_loop_when_configured(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "90000000")
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    pipe = build_pipeline(config, registers, asyncio.Event(), client=object())
    assert len(pipe.coros) == 3  # poller + supervisor + watchdog_loop
    for coro in pipe.coros:
        coro.close()


def test_build_pipeline_omits_watchdog_loop_when_not_configured(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    pipe = build_pipeline(config, registers, asyncio.Event(), client=object())
    assert len(pipe.coros) == 2  # poller + supervisor only
    for coro in pipe.coros:
        coro.close()


async def test_connect_with_retry_touches_heartbeat_each_attempt():
    client = _FlakyClient(fail_times=2)
    hb = Heartbeat()
    ok = await connect_with_retry(client, asyncio.Event(), delay=0.001, max_delay=0.01, heartbeat=hb)
    assert ok is True
    assert hb.age(time.monotonic()) < 1.0  # touched on the connect loop's most recent attempt


async def test_build_pipeline_poller_touches_heartbeat_on_success(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")

    class _FakeClient:
        async def read_holding_registers(self, address, count, slave=0):
            class _Resp:
                registers = [0] * count

                def isError(self):
                    return False

            return _Resp()

    stop = asyncio.Event()
    pipe = build_pipeline(config, registers, stop, client=_FakeClient())
    poller_task = asyncio.create_task(pipe.coros[0])
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(poller_task, timeout=1.0)
    for coro in pipe.coros[1:]:
        coro.close()
    assert pipe.heartbeat.age(time.monotonic()) < 1.0
```

Note: `_FlakyClient` is already defined earlier in `tests/test_app.py` (used by the existing `test_connect_with_retry_succeeds_after_failures` test) — reuse it, don't redefine it.

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: `TypeError: connect_with_retry() got an unexpected keyword argument 'heartbeat'` on `test_connect_with_retry_touches_heartbeat_each_attempt`, and `AttributeError: 'Pipeline' object has no attribute 'heartbeat'` on `test_build_pipeline_poller_touches_heartbeat_on_success` (`app.py` doesn't wire either yet — `Heartbeat` itself imports fine since Task 1 already created it). `test_build_pipeline_adds_watchdog_loop_when_configured` fails on the `len(pipe.coros) == 3` assertion (still 2, `app.py` doesn't add `watchdog_loop` yet). The two renamed existing tests should still pass unchanged (they only gained an unused-so-far `monkeypatch` fixture).

- [ ] **Step 3: Implement the `app.py` changes**

Add the import (after the existing `from .nd45_poller import run_poller` line):

```python
from .nd45_poller import run_poller
from .watchdog import Heartbeat, notify_ready, watchdog_loop, watchdog_seconds
```

Add `heartbeat` to the `Pipeline` dataclass (currently):

```python
@dataclass
class Pipeline:
    """Assembled bridge components shared by `run` and `monitor`."""

    store: CanonicalStore
    context: object
    client: object
    coros: list
```

to:

```python
@dataclass
class Pipeline:
    """Assembled bridge components shared by `run` and `monitor`."""

    store: CanonicalStore
    context: object
    client: object
    coros: list
    heartbeat: Heartbeat
```

Update `connect_with_retry` (currently):

```python
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
```

to:

```python
async def connect_with_retry(
    client, stop_event: asyncio.Event, delay: float = 1.0, max_delay: float = 30.0,
    heartbeat: Heartbeat | None = None,
) -> bool:
    """Keep attempting the initial ND45 connect (backoff) until success or stop.

    pymodbus auto-reconnects a link that was up and dropped, but NOT an initial
    connect that never succeeded (e.g. the service starting before ND45 is
    reachable). This retry loop covers that startup race. Returns True once
    connected, or False if `stop_event` is set before any connection is made.
    """
    current = delay
    while not stop_event.is_set():
        if heartbeat is not None:
            heartbeat.touch(time.monotonic())
        if await client.connect():
            return True
        log.warning("ND45 not reachable; retrying connect in %.1fs", current)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=current)
        except asyncio.TimeoutError:
            pass
        current = min(current * 2, max_delay)
    return False
```

Update `build_pipeline` (currently):

```python
def build_pipeline(
    config: AppConfig,
    registers: RegisterMap,
    stop_event: asyncio.Event,
    activity: RtuActivity | None = None,
    client=None,
) -> Pipeline:
    """Wire poller + DTSU output server + fail-safe. Pass `activity` to record read requests."""
    store = CanonicalStore()
    gate = HealthGate(config.safety.max_data_age_s)
    context = build_context(
        registers.dtsu_target, config.dtsu.slave_id, activity=activity, dtsu_cfg=config.dtsu
    )
    base_on_update = build_on_update(store, context, config.dtsu.slave_id, registers.dtsu_target)
    reporter = FaultReporter()

    def on_update(values: dict[str, float], ts: float) -> None:
        reporter.success()  # a good poll clears any active fault state
        base_on_update(values, ts)

    client = client or AsyncModbusTcpClient(
        config.nd45.host, port=config.nd45.port, timeout=config.nd45.timeout_s
    )
    poller = run_poller(
        client, registers.nd45_source, config.nd45.unit_id,
        config.nd45.poll_interval_s, on_update, reporter.failure, stop_event,
    )
    supervisor = supervise_server(
        config.dtsu, context, store, gate,
        config.safety.check_interval_s, stop_event,
        min_restart_interval=config.safety.min_restart_interval_s,
    )
    return Pipeline(store=store, context=context, client=client, coros=[poller, supervisor])
```

to:

```python
def build_pipeline(
    config: AppConfig,
    registers: RegisterMap,
    stop_event: asyncio.Event,
    activity: RtuActivity | None = None,
    client=None,
) -> Pipeline:
    """Wire poller + DTSU output server + fail-safe. Pass `activity` to record read requests."""
    store = CanonicalStore()
    gate = HealthGate(config.safety.max_data_age_s)
    context = build_context(
        registers.dtsu_target, config.dtsu.slave_id, activity=activity, dtsu_cfg=config.dtsu
    )
    base_on_update = build_on_update(store, context, config.dtsu.slave_id, registers.dtsu_target)
    reporter = FaultReporter()
    heartbeat = Heartbeat()

    def on_update(values: dict[str, float], ts: float) -> None:
        heartbeat.touch(time.monotonic())
        reporter.success()  # a good poll clears any active fault state
        base_on_update(values, ts)

    def on_error(exc: Exception) -> None:
        heartbeat.touch(time.monotonic())
        reporter.failure(exc)

    client = client or AsyncModbusTcpClient(
        config.nd45.host, port=config.nd45.port, timeout=config.nd45.timeout_s
    )
    poller = run_poller(
        client, registers.nd45_source, config.nd45.unit_id,
        config.nd45.poll_interval_s, on_update, on_error, stop_event,
    )
    supervisor = supervise_server(
        config.dtsu, context, store, gate,
        config.safety.check_interval_s, stop_event,
        min_restart_interval=config.safety.min_restart_interval_s,
    )
    coros = [poller, supervisor]
    watchdog_sec = watchdog_seconds()
    if watchdog_sec is not None:
        coros.append(watchdog_loop(heartbeat, watchdog_sec, stop_event))
    return Pipeline(store=store, context=context, client=client, coros=coros, heartbeat=heartbeat)
```

Update `run_app` (currently):

```python
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
```

to:

```python
async def run_app(
    config: AppConfig,
    registers: RegisterMap,
    stop_event: asyncio.Event,
    client=None,
) -> None:
    pipe = build_pipeline(config, registers, stop_event, client=client)
    notify_ready()
    connected = await connect_with_retry(
        pipe.client, stop_event,
        config.nd45.reconnect_delay_s, config.nd45.reconnect_delay_max_s,
        heartbeat=pipe.heartbeat,
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
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: all tests PASS, including the 4 new/changed ones.

- [ ] **Step 5: Run the full suite to check for fallout**

Run: `.venv/bin/python -m pytest -q`
Expected: all tests PASS (this touches shared `app.py`, so `tests/test_monitor.py` and any other consumer of `build_pipeline` must still work — `monitor.py` doesn't reference `Pipeline`'s fields directly beyond `.store`/`.client`/`.coros`, so the new `.heartbeat` field is additive and safe).

- [ ] **Step 6: Lint**

Run: `.venv/bin/python -m ruff check .`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/nd45_dtsu666/app.py tests/test_app.py
git commit -m "feat: tie the watchdog heartbeat to real ND45-poller progress"
```

---

### Task 3: systemd unit + documentation

**Files:**
- Modify: `systemd/nd45-dtsu666.service`
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Interfaces:** None — configuration and documentation only, no code.

- [ ] **Step 1: Update `systemd/nd45-dtsu666.service`**

Replace the `[Service]` section's opening (currently):

```ini
[Service]
Type=simple
WorkingDirectory=/opt/nd45_dtsu666
```

with:

```ini
[Service]
Type=notify
WatchdogSec=90
WorkingDirectory=/opt/nd45_dtsu666
```

(the rest of the file — `ExecStart`, `Restart=always`, `RestartSec=2`, `StandardOutput`/`StandardError`, `[Install]` — stays unchanged).

- [ ] **Step 2: Update `README.md`**

Append this paragraph immediately after the "Run as a service" code block (currently ending at line 69, right before the "## On-site verification checklist" heading):

```markdown
The unit uses systemd's watchdog (`WatchdogSec=90`): the app pings systemd every
~45s as long as the ND45 poller is making progress (connecting, polling
successfully, or handling a poll failure — a real ND45 outage is a normal,
expected state, not a hang). If the poller genuinely freezes for 90s, systemd
stops seeing pings and restarts the service automatically (`Restart=always`
already covers this the same as a crash). No config changes needed to enable
or disable this — it's entirely controlled by `WatchdogSec=` in the unit file.
```

- [ ] **Step 3: Update `CLAUDE.md`**

Add a new bullet to the "Architecture" section, immediately after the existing fail-safe bullet (currently):

```markdown
- **Fail-safe** (`dtsu_server.supervise_server`): when ND45 data is older than `safety.max_data_age_s`, the DTSU output server (RTU or TCP, per `dtsu.transport`) is **stopped** (goes silent) so Sigenergy detects a timeout and enters its own safe mode. It restarts automatically when data returns.
```

insert this new bullet right after it (before the `monitor.py` bullet):

```markdown
- **Watchdog** (`watchdog.py`): the systemd unit's `WatchdogSec=` (read from the `WATCHDOG_USEC` env var, no duplication in `config.json`) drives a heartbeat tied to real ND45-poller progress (`Heartbeat`, touched by `connect_with_retry` and by `build_pipeline`'s `on_update`/`on_error`) — a genuine poller hang stops the pings and lets systemd restart the service; a normal ND45 outage does not, since the poller is still cycling through its error path. See `docs/superpowers/specs/2026-07-06-systemd-watchdog-design.md`.
```

- [ ] **Step 4: Run the full suite and lint as a final check**

Run: `.venv/bin/python -m pytest -q`
Expected: all tests PASS (docs/config-only change, no test impact).

Run: `.venv/bin/python -m ruff check .`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add systemd/nd45-dtsu666.service README.md CLAUDE.md
git commit -m "docs: document systemd watchdog integration; enable WatchdogSec=90"
```
