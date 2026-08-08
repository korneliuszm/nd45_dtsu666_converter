# MQTT measurement publisher — design

Date: 2026-08-08
Status: implemented

## Goal

Push canonical measurements to an MQTT broker on a fixed interval, so consumers that
do not poll Modbus and do not scrape Prometheus (Home Assistant, Node-RED, a
supervising EMS) can receive them.

Both existing interfaces are **pull**: Sigenergy reads the DTSU register map, Grafana
scrapes `/metrics`. Nothing in the process ever initiates. This adds the missing push
channel without touching either.

Scope as requested: broker settings, a configurable list of published values drawn
from the `registers.json` canonical vocabulary (`p_total` to start), a publish period
(0.2 s to start), and an off switch.

## Command and safety boundary

**`run` publishes. Nothing else does.**

`monitor`, `rtudebug`, `static` and `diag` are diagnostics a human runs on a device
where the production service is usually also running. A second client presenting the
same client id would evict the service's session; `static`'s synthetic values would be
indistinguishable from real ones on a dashboard; `diag` builds no pipeline at all.
Wiring MQTT into `run_app` alone makes that structural rather than a rule to remember.

**Publishing follows the fail-safe exactly.** A bridge whose data is older than its own
`safety.max_data_age_s` publishes *no measurement* — the same `HealthGate` and the same
threshold `dtsu_server.supervise_server` uses to stop serving that bridge's output. The
moment Sigenergy stops being fed is the moment MQTT goes quiet. Republishing a
last-known value there would tell a dashboard the opposite of what the battery sees.

**MQTT can never take the bridge down.** The publisher is a task outside `pipe.coros`,
so nothing awaits it until shutdown; a broker outage backs off and retries, and
`publish_loop` catches `Exception`, not just `MqttError`, because an escaping exception
would leave a silently dead publisher behind a process that still looks healthy. If
`aiomqtt` is not installed at all — an upgrade that skipped `pip install -e .` — `start`
logs an ERROR and returns `None`.

## Configuration

**A separate file, `config/mqtt.json`, not a section of `config.json`.** Three reasons:

1. It carries a broker password. `config.json` is the file both systemd instances read
   and the one copied between devices during bring-up.
2. `config.json`'s invariant — "outside `bridges` there are only `prometheus` and
   `static_debug`" — stays true, and the cross-bridge validators that depend on one
   configuration seeing every bridge are untouched.
3. It is the setting most likely to change without any bridge change.

**`AppConfig` gains no field.** The conf is a defaulted keyword on `run_app` only. That
keeps `build_pipeline` (8 parameters, ~30 call sites), `Pipeline`, `BridgeRuntime` and
all seven shipped `config/config*.json` files untouched — `test_shipped_config_files_load`
needed no change, which is the concrete payoff.

**A missing file is not an error.** `load_mqtt_config` returns `None`, `__main__` logs one
INFO line and substitutes a disabled `MqttConf`. Every existing deployment, every
`config_debug_*.json` run and every test invocation predates this file and must not
change behaviour by upgrading. A hard failure would turn a missing file into a
crash-loop: the unit sets `Restart=always` with `StartLimitIntervalSec=0`, so it would
retry every 5 s forever with the Power Sensor unserved. A file that *is* present but
malformed still raises — a typo'd broker password must fail loudly.

**Point names are validated at load** against `MQTT_POINT_KEYS`, the same mechanism
`StaticDebugConf._validate_values` uses, with the same message shape (`unknown mqtt
point(s): …`). Deferring the check to publish time would let the process start,
connect, and publish `{"ts": …}` with no values while looking healthy. The set is
`STATIC_DEBUG_VALUE_KEYS | DERIVED_CANONICAL_KEYS` — producible by every source
section, so a name from it works whichever bridge the process runs — plus `dc_power`
and `e_daily`, which exist only on `huawei_plant_source`. Those two are allowed rather
than rejected so a SmartLogger deployment needs no code change; a bridge that does not
report a configured point omits it from the payload and warns **once**, not at 5 Hz.

**The default path is `mqtt.json` beside `--config`**, not `config/mqtt.json` relative to
the CWD. The unit passes an absolute `--config`, so the file is found with no new
`ExecStart` argument — which matters twice over: on this host `/etc` is a tmpfs overlay
(`DEPLOY.md` §0.4) so unit edits need `overlayroot-chroot` or they evaporate on reboot,
and a `--mqtt` flag baked into the unit would break a symlink rollback to a checkout
that predates this feature (`unrecognized arguments: --mqtt`, bridge dead).

## Architecture

New module `mqtt_publisher.py`, deliberately shaped like `metrics.py`: reference-holding
dataclasses (`BridgePublish`, `MqttSource`), a pure builder taking its clocks as
arguments, a never-raising async loop, and a `start() -> Task | None` / `async stop(task)`
pair. `run_app` starts it next to `metrics.start` — **before** `connect_with_retry`, for
the same reason: a publisher that only appears once the source connects is absent during
exactly the outage its availability topic exists to report.

**One client per process, every bridge that process runs.** Topics are
`<topic_prefix>/<bridge>/measurements`, so this is identical under `run` (all bridges)
and `run --bridge nd45` (systemd, one).

**Timer-driven, never `on_update`-driven.** `on_update` is a synchronous callback on the
poll loop's critical path, where its ordering is already load-bearing and a broker
round-trip has no business being. ND45 also polls at 20 Hz, so an update-driven
publisher would emit 20 msg/s per bridge and make the configured interval meaningless.
Decoupling means one interval covers bridges polling at 0.05 s, 0.2 s and 1.0 s; a source
slower than the interval repeats a sample, which is visible because `ts` belongs to the
sample.

Scheduling is **deadline-based** (`deadline += interval`), not `sleep(interval)`: a fixed
sleep adds the publish work and the broker round-trip to every period, so the cadence
walks off over hours. Falling a whole period behind resynchronises rather than firing a
catch-up burst — a backlog of 200 ms-old measurements has no value. Measured against a
real broker: 200 ms median gap, no drift over the run.

**Three clocks, and mixing them is the easy mistake.** Freshness and scheduling use the
asyncio **loop** clock, because that is what the pollers stamp into `CanonicalStore`. The
payload `ts` is **wall** clock, because a loop-clock number is seconds-since-process-start
and means nothing to a subscriber — it is derived as `wall_now - age` and never read from
the store, which also keeps the timestamp on the *sample* rather than on the message.
`build_payload` takes both as arguments and calls neither, so it is testable without a
loop, the same discipline as `metrics.render`.

### Client identity

`MqttConf.client_id_for(only)` appends the bridge name: `nd45-dtsu666-nd45`,
`nd45-dtsu666-etango`. MQTT allows one connection per client id, so a shared id would
have the broker evict the incumbent, which reconnects and evicts the newcomer, forever.
This is the MQTT shape of the `prometheus.port` collision and is solved the same way —
`client_id_for` mirrors `AppConfig.metrics_port_for`. The availability topic is
discriminated by the same `only`, so one instance's Will cannot overwrite the other's
`online`.

### Availability vs. status

| Topic | Payload | Retained | Reports |
|---|---|---|---|
| `<prefix>/<bridge>/measurements` | JSON | no | a sample |
| `<prefix>/<bridge>/status` | `fresh` \| `stale` | yes | this bridge's data freshness |
| `<prefix>/<only>/availability` | `online` \| `offline` | yes | this process's liveness |

Two topics because there are two distinct failures. `availability` going `offline` is the
broker delivering the Last Will: the process died or the connection dropped. `status`
going `stale` means the process is alive and connected but its source has gone quiet —
the state in which the DTSU output is already silenced. Consumer rule: trust
`measurements` only while `availability` is `online` **and** `status` is `fresh`.

`status` is edge-triggered, so a steady bridge does not emit 5 status messages a second;
a reconnect clears the remembered edge so the next session republishes it. Measurements
are **not** retained by default — a retained 0.2 s sample is served to every new
subscriber forever, long after it stopped being true, which is the staleness the
freshness gate exists to prevent, moved into the broker.

### aiomqtt

`aiomqtt>=2.0,<3`, chosen over bare `paho-mqtt` because `loop_start()` is a thread and
this process is one event loop with no locks — the same reason `metrics.py` rejected
`prometheus_client`. Pinned below 3 because 1.x→2.x already renamed `client_id` to
`identifier`.

Two API facts the implementation depends on: `Client.__init__` calls
`asyncio.get_running_loop()`, so the client is built inside `publish_loop`, never in
`start()` or at config-load time; and **2.x does not reconnect on its own** inside the
context manager, so the reconnect is an outer loop with exponential backoff — the pattern
its own docs prescribe, and the same shape as `app.connect_with_retry`. A fresh client is
built per attempt: the context manager is reusable but not reentrant, and at a 2–30 s
backoff rebuilding costs nothing while keeping `client_factory` a clean test seam.

At QoS 0 `publish()` returns once paho has queued the frame, so the intended 5 Hz stream
never waits on the broker. QoS ≥ 1 awaits an ack bounded by `publish_timeout_s`.

## Verification

Beyond the unit tests (`tests/test_mqtt.py`, plus cases in `test_main.py`,
`test_app.py`, `test_bridge_isolation.py`), the following was exercised against a real
MQTT broker with a real `aiomqtt` client and a faked Modbus source:

- measurements published on the expected topic with `p_total` and a wall-clock `ts`;
- `availability` → `online` on connect, `offline` on shutdown;
- `status` → `fresh`, then `stale` when the source stopped answering, with measurements
  ceasing at the `max_data_age_s` boundary and not before;
- cadence 200 ms median with no cumulative drift;
- two instances with different client ids publishing concurrently, no eviction flap;
- broker killed mid-run: publisher survived, backed off, reconnected on its own and
  resumed publishing without intervention.

## Known limitations

- **TLS is designed but untested.** `broker.tls` selects `aiomqtt.TLSParameters()` with
  defaults, i.e. the system CA store. A site needing a private CA or client certificates
  will need `ca_certs`/`certfile`/`keyfile` fields added; they were left out rather than
  shipped unverified.
- **The password is plaintext in the file.** Never log the model — only host, port and
  client id, as `publish_loop` does. `config/mqtt.local.json` is gitignored and DEPLOY
  §3.1 says to `chmod 600` a file that holds one. `pydantic.SecretStr` is the stricter
  option and is a reasonable follow-up.
- **The device may have no RTC**, so `ts` jumps when NTP first syncs. A subscriber that
  cares about liveness should key off `status`/`availability`, not `ts`.
