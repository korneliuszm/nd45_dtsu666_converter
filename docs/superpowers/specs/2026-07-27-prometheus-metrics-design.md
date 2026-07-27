# Prometheus Metrics Endpoint Design

## Goal

Make the bridge's state verifiable remotely and continuously, without SSH-ing into
the reComputer and running `monitor`. An operator (or Grafana) must be able to answer,
from a single scrape:

- is the ND45 connected, and how old is its data;
- is the fail-safe silencing the output right now;
- is Sigenergy actually reading the DTSU666 registers, and which ones;
- what value sits in each output register at this moment.

## Command and safety boundary

There is no new command. The endpoint is part of `run`, `monitor`, `rtudebug`, and
`static`, controlled by configuration. It is **read-only**: it serves `GET` and has
no path that writes to the canonical store, the datastore, or the ND45.

## Configuration

`config/config.json` gains an optional section:

```json
"prometheus": {"enabled": true, "host": "0.0.0.0", "port": 9090, "include_registers": true}
```

The section is optional and every field has a default, so existing configuration
files (including the six `config_debug_*.json` variants) keep loading unchanged.

`port` is validated to 1..65535. Configuration is rejected when `dtsu.transport` is
`"tcp"` and the metrics port equals `dtsu.tcp.port` on an overlapping bind address:
otherwise the Modbus TCP listener simply fails to bind and the bridge sits in
fail-safe for a reason that is not obvious from the logs.

`--metrics-port N` and `--no-metrics` override the loaded configuration, for running
a second instance during bring-up.

## Architecture

The exporter is hand-rolled on `asyncio.start_server` rather than built on
`prometheus_client`. That library's `start_http_server` runs its own thread, while
this process is a single asyncio loop with no locks. Everything the exporter needs
is already held in objects the bridge maintains, and it reads them at scrape time,
so there is no shared mutable state to guard and no new runtime dependency to install
on an offline device.

Reused unchanged:

- `CanonicalStore.snapshot()` / `.age()` — latest SI values and data age;
- `RtuActivity.summary()` / `.last_seen()` — Sigenergy request counts, rate, and
  per-register last-read timestamps;
- `encode_target_point()` + `registers_to_float()` — the same encode path
  `update_datastore` uses, so a register gauge reports what Sigenergy would read;
- `Heartbeat.age()` — watchdog liveness.

Two small state holders are new, because the corresponding state exists only as
local variables today:

- `PollStats` — ND45 poll outcome counters, fed from `build_pipeline`'s existing
  `on_update`/`on_error` wrappers. The poller itself is not modified. It is separate
  from `FaultReporter`, whose state is about suppressing log noise, not about being
  read.
- `ServerStatus` — DTSU output server up/down transitions, recorded by
  `supervise_server` through a new optional `status=` parameter. This cannot be
  re-derived at scrape time from the freshness gate: a server whose transport never
  opened (missing or busy serial port) still passes `gate.should_serve`, which is
  exactly the failure the metric has to make visible.

`MetricsSource` bundles the references and is returned on `Pipeline`. It is `None`
when the endpoint is disabled.

### Two clocks

Data age uses the asyncio loop clock (`loop.time()`, what the poller stamps into the
store); Sigenergy request ages use `time.monotonic()` (what `RecordingSlaveContext`
stamps). `render()` takes both as arguments and never calls a clock itself, which
keeps each metric on its own clock and makes the renderer testable without a loop.

### Request activity in `run`

Read-request metrics require the recording datastore context, which until now only
the debug modes enabled. `build_pipeline` therefore creates an `RtuActivity` itself
when the endpoint is enabled and the caller passed none. An explicit `activity=`
(monitor, rtudebug) still wins, and disabling the endpoint restores the previous
plain context exactly.

### Startup ordering

The metrics task is created **before** `connect_with_retry`, alongside the watchdog
task. `pipe.coros` only start running once the ND45 connection succeeds; an endpoint
started with them would be unreachable during an ND45-unreachable-at-startup — the
outage it most needs to report.

## Exposed metrics

All names are prefixed `nd45_dtsu666_`.

| Metric | Meaning |
|---|---|
| `build_info{version,mode,transport,dtsu_slave_id}` | identification; `mode` distinguishes `static` synthetic data |
| `uptime_seconds` | exporter uptime |
| `nd45_connected` | ND45 TCP link state (omitted when there is no client) |
| `nd45_data_age_seconds`, `nd45_data_fresh`, `nd45_max_data_age_seconds` | freshness against the fail-safe threshold |
| `nd45_poll_interval_seconds` | configured poll interval |
| `nd45_polls_total{result}` | poll attempts by outcome |
| `nd45_consecutive_poll_failures` | failures since the last success |
| `nd45_last_successful_poll_age_seconds`, `nd45_last_poll_error_age_seconds` | poll recency |
| `nd45_last_poll_error_info{type}` | exception type of the last failure |
| `watchdog_heartbeat_age_seconds` | poller liveness as the systemd watchdog sees it |
| `dtsu_server_up`, `dtsu_server_starts_total`, `dtsu_server_stops_total`, `dtsu_server_last_start_age_seconds` | output transport state |
| `dtsu_requests_total`, `dtsu_request_rate_per_second`, `dtsu_last_request_age_seconds` | Sigenergy read activity |
| `dtsu_block_reads_total{fc,addr,count}` | per-block read counts |
| `canonical_value{point}` | latest SI measurement per canonical point |
| `dtsu_register_value{map,point,fc,addr}` | value currently encoded in each output register |
| `dtsu_register_read_age_seconds{map,point,fc,addr}` | seconds since Sigenergy last read that register |

The last three families are dropped when `include_registers` is `false`.

`canonical_value` is one family with a `point` label rather than per-unit families
(`*_volts`, `*_watts`). The point set is defined by `registers.json` and changes
without touching code; splitting it would require a units table in the source. This
is a diagnostic exporter, and the mixed units are documented rather than encoded in
metric names.

Label cardinality is bounded: canonical points and register points come from the
register map, and `RtuActivity` already caps distinct tracked blocks at 64. Poll
error **messages** are deliberately not exported — only the exception type — since
messages embed addresses and values. The message stays in the journal.

Values that cannot be encoded as float32 (an out-of-range SI reading, which leaves
the datastore holding its previous image) are skipped for that one register instead
of failing the scrape.

## Endpoints

- `GET /metrics` — exposition text, `text/plain; version=0.0.4`
- `GET /healthz` — `200 ok` while ND45 data is fresh, `503 stale` otherwise
- `GET /` — a short index
- anything else — `404`

## Error handling

A bind failure (port busy, invalid `--metrics-port` override) is logged and retried
every 30 s; it never raises out of the task and never affects the bridge. Request
handling is wrapped so a malformed request or a scraper that disconnects mid-response
degrades to a log line. Reads are bounded in time and in header count. Responses
always close the connection.

If the endpoint is disabled, no task is created and no socket is opened.

## Testing

Automated tests verify:

- non-finite formatting (`+Inf`, `-Inf`, `NaN`) and label escaping;
- every emitted sample line has a `# TYPE` declaration;
- an empty store renders `+Inf` age and `nd45_data_fresh 0`;
- data age and request age move independently (the two clocks);
- `nd45_connected` is omitted without a client and reported with one;
- poll counters advance through `build_pipeline`'s callbacks with a fake client, and
  the error message never reaches the output;
- `ServerStatus` follows `supervise_server` through a start and a fail-safe stop,
  with an injected server factory and clock;
- register gauges match the datastore encode path, and read ages go from `+Inf` to
  finite once a register is read;
- `include_registers=false` drops the value families and keeps the health ones;
- an unencodable value skips only its own register;
- request parsing and routing for `/metrics`, `/healthz` (fresh and stale), `/`, and
  unknown paths;
- a failed bind retries rather than raising;
- configuration defaults, the optional section, port range, and the `dtsu.tcp.port`
  collision.

Per the project's testing rule, no test opens a network socket: the HTTP layer is
covered through its pure `parse_request` / `route` / `_response` functions.

## Non-goals

- Authentication, TLS, or rate limiting. The endpoint is read-only diagnostics on a
  private LAN; restrict it with `prometheus.host` or a firewall if that changes.
- Push/remote-write, alerting rules, or a bundled Grafana dashboard.
- Histograms or quantiles of poll latency.
- Replacing `monitor` / `rtudebug`, which stay the interactive bring-up tools.
