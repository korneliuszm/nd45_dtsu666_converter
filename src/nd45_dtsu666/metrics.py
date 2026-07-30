"""Prometheus scrape endpoint: bridge health, canonical SI values, DTSU registers.

Read-only diagnostics served over plain HTTP so the bridge's state can be checked
from Grafana instead of by SSH-ing into the device and running `monitor`.

Deliberately hand-rolled on `asyncio.start_server` rather than `prometheus_client`:
that library's `start_http_server` runs its own thread, while this process is a
single asyncio loop with no locks (see CLAUDE.md). Everything here reads the same
objects the bridge already keeps -- `CanonicalStore`, `RtuActivity`, `Heartbeat` --
at scrape time, so there is no shared mutable state to guard.

TWO CLOCKS (CLAUDE.md): data age comes from the asyncio loop clock (`loop.time()`,
what the poller stamps into the store); Sigenergy request ages come from
`time.monotonic()` (what `RecordingSlaveContext` stamps). `render()` takes both
explicitly and never calls a clock itself, which also keeps it testable without a
running loop.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field

from . import __version__
from .canonical import CanonicalStore
from .codec import registers_to_float
from .config import AppConfig, PrometheusConf, TargetSide
from .dtsu_server import RtuActivity, encode_target_point
from .watchdog import Heartbeat

log = logging.getLogger(__name__)

PREFIX = "nd45_dtsu666"
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_MAX_HEADER_LINES = 64
_READ_TIMEOUT_S = 5.0
_REBIND_DELAY_S = 30.0


class PollStats:
    """ND45 poll outcome counters, fed from build_pipeline's on_update/on_error.

    Separate from `FaultReporter`, which owns log-noise suppression and whose
    state is about logging transitions, not about being read.
    """

    def __init__(self) -> None:
        self.polls_ok = 0
        self.polls_failed = 0
        self.consecutive_failures = 0
        self.last_ok_ts: float | None = None
        self.last_error_ts: float | None = None
        self.last_error_type: str | None = None

    def record_ok(self, ts: float) -> None:
        self.polls_ok += 1
        self.consecutive_failures = 0
        self.last_ok_ts = ts

    def record_error(self, exc: BaseException, ts: float) -> None:
        self.polls_failed += 1
        self.consecutive_failures += 1
        self.last_error_ts = ts
        # type only: the message can embed addresses/values and would blow up
        # label cardinality. The full text stays in the journal.
        self.last_error_type = type(exc).__name__


class ServerStatus:
    """Whether the DTSU output server is actually up, as seen by its supervisor.

    Not derivable from the freshness gate: a server whose transport never opened
    (missing/busy serial port) looks 'should serve' but is serving nothing --
    exactly the failure this metric has to make visible.
    """

    def __init__(self) -> None:
        self.running = False
        self.starts = 0
        self.stops = 0
        self.last_start_ts: float | None = None

    def started(self, ts: float) -> None:
        self.running = True
        self.starts += 1
        self.last_start_ts = ts

    def stopped(self) -> None:
        if self.running:
            self.stops += 1
        self.running = False


@dataclass
class MetricsSource:
    """Everything the exporter reads. Holds references, never copies of state."""

    config: AppConfig
    store: CanonicalStore
    activity: RtuActivity
    poll_stats: PollStats
    server_status: ServerStatus
    # (map name, side) pairs -- the map name becomes a metric label
    targets: list[tuple[str, TargetSide]] = field(default_factory=list)
    heartbeat: Heartbeat | None = None
    client: object | None = None
    mode: str = "run"
    started_at: float = field(default_factory=time.monotonic)
    # Secondary Huawei SmartLogger source; all None unless huawei.enabled.
    # Reported under separate `source`-labelled families and judged against
    # huawei.max_data_age_s, never safety.max_data_age_s -- this source does not
    # gate the DTSU output.
    huawei_store: CanonicalStore | None = None
    huawei_poll_stats: PollStats | None = None
    huawei_client: object | None = None


def _fmt(value: float) -> str:
    """Format a float in Prometheus exposition syntax."""
    if math.isnan(value):
        return "NaN"
    if value == math.inf:
        return "+Inf"
    if value == -math.inf:
        return "-Inf"
    value = float(value)
    # counters and 0/1 gauges read better without a trailing ".0"; Prometheus
    # parses both forms identically
    if value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{name}="{_esc(value)}"' for name, value in pairs)
    return "{" + inner + "}"


class _Out:
    """Accumulates exposition lines, emitting HELP/TYPE once per family."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._declared: set[str] = set()

    def family(self, name: str, kind: str, help_text: str) -> None:
        if name in self._declared:
            return
        self._declared.add(name)
        self._lines.append(f"# HELP {name} {help_text}")
        self._lines.append(f"# TYPE {name} {kind}")

    def sample(self, name: str, value: float, labels: list[tuple[str, str]] | None = None) -> None:
        self._lines.append(f"{name}{_labels(labels or [])} {_fmt(value)}")

    def text(self) -> str:
        return "\n".join(self._lines) + "\n"


def _age(last: float | None, now: float) -> float:
    return math.inf if last is None else now - last


def render(source: MetricsSource, loop_now: float, mono_now: float) -> str:
    """Render the full exposition text. Pure: both clocks are passed in."""
    out = _Out()
    cfg = source.config
    dtsu = cfg.dtsu

    out.family(f"{PREFIX}_build_info", "gauge", "Bridge build and mode identification.")
    out.sample(
        f"{PREFIX}_build_info",
        1,
        [
            ("version", __version__),
            ("mode", source.mode),
            ("transport", dtsu.transport),
            ("dtsu_slave_id", str(dtsu.slave_id)),
        ],
    )
    out.family(f"{PREFIX}_uptime_seconds", "gauge", "Seconds since the exporter started.")
    out.sample(f"{PREFIX}_uptime_seconds", mono_now - source.started_at)

    _render_nd45(out, source, loop_now, mono_now)
    _render_secondary_sources(out, source, loop_now, mono_now)
    _render_dtsu(out, source, mono_now)
    if cfg.prometheus.include_registers:
        _render_values(out, source, mono_now)
    return out.text()


def _render_nd45(out: _Out, source: MetricsSource, loop_now: float, mono_now: float) -> None:
    cfg = source.config
    stats = source.poll_stats
    age = source.store.age(loop_now)  # loop clock: the poller stamps loop.time()

    connected = getattr(source.client, "connected", None)
    if connected is not None:
        out.family(
            f"{PREFIX}_nd45_connected", "gauge", "1 if the ND45 Modbus TCP link is up."
        )
        out.sample(f"{PREFIX}_nd45_connected", 1 if connected else 0)

    out.family(
        f"{PREFIX}_nd45_data_age_seconds",
        "gauge",
        "Age of the newest decoded ND45 sample. +Inf until the first poll.",
    )
    out.sample(f"{PREFIX}_nd45_data_age_seconds", age)
    out.family(
        f"{PREFIX}_nd45_data_fresh",
        "gauge",
        "1 while ND45 data is within safety.max_data_age_s (fail-safe not tripped).",
    )
    out.sample(f"{PREFIX}_nd45_data_fresh", 1 if age <= cfg.safety.max_data_age_s else 0)
    out.family(
        f"{PREFIX}_nd45_max_data_age_seconds",
        "gauge",
        "Configured safety.max_data_age_s threshold.",
    )
    out.sample(f"{PREFIX}_nd45_max_data_age_seconds", cfg.safety.max_data_age_s)
    out.family(
        f"{PREFIX}_nd45_poll_interval_seconds", "gauge", "Configured ND45 poll interval."
    )
    out.sample(f"{PREFIX}_nd45_poll_interval_seconds", cfg.nd45.poll_interval_s)

    out.family(f"{PREFIX}_nd45_polls_total", "counter", "ND45 poll attempts by outcome.")
    out.sample(f"{PREFIX}_nd45_polls_total", stats.polls_ok, [("result", "ok")])
    out.sample(f"{PREFIX}_nd45_polls_total", stats.polls_failed, [("result", "error")])
    out.family(
        f"{PREFIX}_nd45_consecutive_poll_failures",
        "gauge",
        "Failed ND45 polls since the last successful one.",
    )
    out.sample(f"{PREFIX}_nd45_consecutive_poll_failures", stats.consecutive_failures)
    out.family(
        f"{PREFIX}_nd45_last_successful_poll_age_seconds",
        "gauge",
        "Seconds since the last successful ND45 poll. +Inf if there was none.",
    )
    out.sample(
        f"{PREFIX}_nd45_last_successful_poll_age_seconds", _age(stats.last_ok_ts, mono_now)
    )
    out.family(
        f"{PREFIX}_nd45_last_poll_error_age_seconds",
        "gauge",
        "Seconds since the last failed ND45 poll. +Inf if there was none.",
    )
    out.sample(
        f"{PREFIX}_nd45_last_poll_error_age_seconds", _age(stats.last_error_ts, mono_now)
    )
    if stats.last_error_type is not None:
        out.family(
            f"{PREFIX}_nd45_last_poll_error_info",
            "gauge",
            "Exception type of the last failed ND45 poll (message stays in the journal).",
        )
        out.sample(
            f"{PREFIX}_nd45_last_poll_error_info", 1, [("type", stats.last_error_type)]
        )

    if source.heartbeat is not None:
        out.family(
            f"{PREFIX}_watchdog_heartbeat_age_seconds",
            "gauge",
            "Seconds since the poller last made progress (drives the systemd watchdog).",
        )
        out.sample(
            f"{PREFIX}_watchdog_heartbeat_age_seconds", source.heartbeat.age(mono_now)
        )


def _render_secondary_sources(
    out: _Out, source: MetricsSource, loop_now: float, mono_now: float
) -> None:
    """Emit `source`-labelled families for non-primary data sources.

    A separate labelled family set rather than more `nd45_*` metrics: the
    existing families are what the deployed dashboards scrape, and a secondary
    source must not change their meaning. Nothing is emitted at all unless
    huawei.enabled, so a single-source bridge's scrape is byte-identical to before.
    """
    if source.huawei_store is None or source.huawei_poll_stats is None:
        return
    cfg = source.config
    label = [("source", "huawei")]
    age = source.huawei_store.age(loop_now)  # loop clock, as the poller stamps it
    stats = source.huawei_poll_stats

    connected = getattr(source.huawei_client, "connected", None)
    if connected is not None:
        out.family(
            f"{PREFIX}_source_connected",
            "gauge",
            "1 if the secondary source's Modbus TCP link is up.",
        )
        out.sample(f"{PREFIX}_source_connected", 1 if connected else 0, label)

    out.family(
        f"{PREFIX}_source_data_age_seconds",
        "gauge",
        "Age of the newest sample from a secondary source. +Inf until the first poll.",
    )
    out.sample(f"{PREFIX}_source_data_age_seconds", age, label)
    out.family(
        f"{PREFIX}_source_data_fresh",
        "gauge",
        "1 while a secondary source is within its own max_data_age_s. "
        "Does NOT gate the DTSU output -- only the primary source does.",
    )
    out.sample(
        f"{PREFIX}_source_data_fresh", 1 if age <= cfg.huawei.max_data_age_s else 0, label
    )
    out.family(
        f"{PREFIX}_source_max_data_age_seconds",
        "gauge",
        "Configured staleness threshold for a secondary source.",
    )
    out.sample(f"{PREFIX}_source_max_data_age_seconds", cfg.huawei.max_data_age_s, label)
    out.family(
        f"{PREFIX}_source_poll_interval_seconds",
        "gauge",
        "Configured poll interval for a secondary source.",
    )
    out.sample(f"{PREFIX}_source_poll_interval_seconds", cfg.huawei.poll_interval_s, label)
    out.family(
        f"{PREFIX}_source_polls_total", "counter", "Secondary-source poll attempts by outcome."
    )
    out.sample(f"{PREFIX}_source_polls_total", stats.polls_ok, [*label, ("result", "ok")])
    out.sample(
        f"{PREFIX}_source_polls_total", stats.polls_failed, [*label, ("result", "error")]
    )
    out.family(
        f"{PREFIX}_source_consecutive_poll_failures",
        "gauge",
        "Failed secondary-source polls since the last successful one.",
    )
    out.sample(f"{PREFIX}_source_consecutive_poll_failures", stats.consecutive_failures, label)
    out.family(
        f"{PREFIX}_source_last_successful_poll_age_seconds",
        "gauge",
        "Seconds since the last successful secondary-source poll. +Inf if there was none.",
    )
    out.sample(
        f"{PREFIX}_source_last_successful_poll_age_seconds",
        _age(stats.last_ok_ts, mono_now),
        label,
    )
    if stats.last_error_type is not None:
        out.family(
            f"{PREFIX}_source_last_poll_error_info",
            "gauge",
            "Exception type of the last failed secondary-source poll.",
        )
        out.sample(
            f"{PREFIX}_source_last_poll_error_info",
            1,
            [*label, ("type", stats.last_error_type)],
        )


def _render_dtsu(out: _Out, source: MetricsSource, mono_now: float) -> None:
    status = source.server_status
    summary = source.activity.summary(mono_now)  # monotonic clock: request timing

    out.family(
        f"{PREFIX}_dtsu_server_up",
        "gauge",
        "1 while the DTSU666 output server is running (0 = fail-safe silence or transport down).",
    )
    out.sample(f"{PREFIX}_dtsu_server_up", 1 if status.running else 0)
    out.family(
        f"{PREFIX}_dtsu_server_starts_total", "counter", "DTSU output server starts."
    )
    out.sample(f"{PREFIX}_dtsu_server_starts_total", status.starts)
    out.family(
        f"{PREFIX}_dtsu_server_stops_total",
        "counter",
        "DTSU output server stops (fail-safe silencing or transport failure).",
    )
    out.sample(f"{PREFIX}_dtsu_server_stops_total", status.stops)
    out.family(
        f"{PREFIX}_dtsu_server_last_start_age_seconds",
        "gauge",
        "Seconds since the DTSU output server was last started. +Inf if never.",
    )
    out.sample(
        f"{PREFIX}_dtsu_server_last_start_age_seconds", _age(status.last_start_ts, mono_now)
    )

    out.family(
        f"{PREFIX}_dtsu_requests_total",
        "counter",
        "Modbus read requests received from Sigenergy.",
    )
    out.sample(f"{PREFIX}_dtsu_requests_total", summary["total"])
    out.family(
        f"{PREFIX}_dtsu_request_rate_per_second",
        "gauge",
        "Sigenergy read requests per second over RtuActivity's sliding window.",
    )
    out.sample(f"{PREFIX}_dtsu_request_rate_per_second", summary["rate"])
    out.family(
        f"{PREFIX}_dtsu_last_request_age_seconds",
        "gauge",
        "Seconds since the last Sigenergy read request. +Inf if there was none.",
    )
    last_age = summary["last_seen_age"]
    out.sample(
        f"{PREFIX}_dtsu_last_request_age_seconds", math.inf if last_age is None else last_age
    )

    out.family(
        f"{PREFIX}_dtsu_block_reads_total",
        "counter",
        "Sigenergy read requests by register block (capped at 64 distinct blocks).",
    )
    for (fc, addr, count), hits in summary["blocks"]:
        out.sample(
            f"{PREFIX}_dtsu_block_reads_total",
            hits,
            [("fc", str(fc)), ("addr", str(addr)), ("count", str(count))],
        )


def _render_values(out: _Out, source: MetricsSource, mono_now: float) -> None:
    values, _ = source.store.snapshot()
    ct_ratio = source.config.dtsu.identity.ir_at

    out.family(
        f"{PREFIX}_canonical_value",
        "gauge",
        "Latest canonical SI measurement by point name (mixed units; see registers.json).",
    )
    for name in sorted(values):
        out.sample(f"{PREFIX}_canonical_value", values[name], [("point", name)])

    out.family(
        f"{PREFIX}_dtsu_register_value",
        "gauge",
        "Value currently encoded in each DTSU output register, in that register's own scale.",
    )
    out.family(
        f"{PREFIX}_dtsu_register_read_age_seconds",
        "gauge",
        "Seconds since Sigenergy last read a register. +Inf if it never has.",
    )
    for map_name, side in source.targets:
        for point_name, pt in sorted(side.points.items(), key=lambda kv: kv[1].addr):
            labels = [
                ("map", map_name),
                ("point", point_name),
                ("fc", str(side.function_code)),
                ("addr", str(pt.addr)),
            ]
            out.sample(
                f"{PREFIX}_dtsu_register_read_age_seconds",
                _age(source.activity.last_seen(side.function_code, pt.addr), mono_now),
                labels,
            )
            si = values.get(pt.from_)
            if si is None:
                continue
            try:
                regs = encode_target_point(si, pt, side, ct_ratio=ct_ratio)
            except ValueError:
                # unrepresentable float32: the datastore kept its previous image,
                # so there is no current value to report for this point
                continue
            raw = registers_to_float(regs, side.word_order, side.byte_order)
            out.sample(f"{PREFIX}_dtsu_register_value", raw, labels)


# --- HTTP layer ------------------------------------------------------------

_STATUS_TEXT = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}

_INDEX = (
    "nd45_dtsu666 bridge\n"
    "\n"
    "  /metrics   Prometheus exposition\n"
    "  /healthz   ok / stale (503 when ND45 data is stale)\n"
)


def parse_request(head: bytes) -> str | None:
    """Extract the request target from an HTTP request line. None if unparseable."""
    try:
        line = head.decode("latin-1").strip()
    except UnicodeDecodeError:
        return None
    parts = line.split()
    if len(parts) < 2 or parts[0].upper() != "GET":
        return None
    return parts[1].split("?", 1)[0]


def route(
    path: str | None, source: MetricsSource, loop_now: float, mono_now: float
) -> tuple[int, str, str]:
    """Map a request path to (status, content type, body). Pure."""
    if path == "/metrics":
        return 200, CONTENT_TYPE, render(source, loop_now, mono_now)
    if path == "/healthz":
        age = source.store.age(loop_now)
        if age <= source.config.safety.max_data_age_s:
            return 200, "text/plain; charset=utf-8", f"ok (data age {age:.2f}s)\n"
        return 503, "text/plain; charset=utf-8", f"stale (data age {age:.2f}s)\n"
    if path == "/":
        return 200, "text/plain; charset=utf-8", _INDEX
    return 404, "text/plain; charset=utf-8", "not found\n"


def _response(status: int, content_type: str, body: str) -> bytes:
    payload = body.encode("utf-8")
    head = (
        f"HTTP/1.1 {status} {_STATUS_TEXT.get(status, 'OK')}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    return head.encode("latin-1") + payload


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    """Read the request line and drain headers, bounded in time and line count."""
    request_line = await asyncio.wait_for(reader.readline(), _READ_TIMEOUT_S)
    for _ in range(_MAX_HEADER_LINES):
        line = await asyncio.wait_for(reader.readline(), _READ_TIMEOUT_S)
        if line in (b"\r\n", b"\n", b""):
            break
    return request_line


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, source) -> None:
    try:
        head = await _read_head(reader)
        loop_now = asyncio.get_running_loop().time()
        status, content_type, body = route(
            parse_request(head), source, loop_now, time.monotonic()
        )
        writer.write(_response(status, content_type, body))
        await writer.drain()
    except Exception as exc:  # noqa: BLE001 - a bad scrape must never touch the bridge
        log.warning("Metrics request failed: %r", exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - the peer may already be gone
            pass


async def serve_metrics(
    conf: PrometheusConf,
    source: MetricsSource,
    stop_event: asyncio.Event,
    rebind_delay: float = _REBIND_DELAY_S,
) -> None:
    """Serve /metrics until `stop_event`. Never raises: a bind failure retries."""
    while not stop_event.is_set():
        try:
            server = await asyncio.start_server(
                lambda r, w: _handle(r, w, source), conf.host, conf.port
            )
        # ValueError/OverflowError cover a bad --metrics-port override, which
        # bypasses pydantic's range check by assigning to the loaded model.
        except (OSError, OverflowError, ValueError) as exc:
            log.error(
                "Metrics endpoint could not bind %s:%d (%r); retrying in %.0fs",
                conf.host, conf.port, exc, rebind_delay,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=rebind_delay)
            except asyncio.TimeoutError:
                pass
            continue
        log.info("Metrics endpoint listening on http://%s:%d/metrics", conf.host, conf.port)
        try:
            await stop_event.wait()
        finally:
            server.close()
            try:
                await server.wait_closed()
            except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
                log.warning("Error closing metrics endpoint: %r", exc)
        return


def start(
    config: AppConfig, source: MetricsSource | None, stop_event: asyncio.Event
) -> asyncio.Task | None:
    """Start the metrics endpoint as a task, or None if disabled.

    Started eagerly by each runner BEFORE connect_with_retry: an endpoint that
    only came up after the ND45 connected would be unreachable in exactly the
    outage it exists to diagnose.
    """
    if source is None or not config.prometheus.enabled:
        return None
    return asyncio.create_task(serve_metrics(config.prometheus, source, stop_event))


async def stop(task: asyncio.Task | None) -> None:
    """Cancel a task from `start` and retrieve its outcome."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001 - best-effort shutdown logging
        log.warning("Metrics endpoint task raised while shutting down: %r", exc)
