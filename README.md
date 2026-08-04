# nd45_dtsu666_converter

Temporary Modbus bridge: one or more independent source → DTSU666 translators, each
served as Modbus RTU or Modbus TCP (config-selectable) so a Sigenergy storage system
can read it as a "Power Sensor". The deployment here runs two, **as separate systemd
services**: a Lumel ND45 on `/dev/ttyAMA2` (grid-tie balance) and a Huawei
SmartLogger on `/dev/ttyAMA4` (PV farm production). See "Independent bridges".

The output exposes both the standard DTSU666 holding-register map over FC03 (secondary/
CT side, `0x2000`/`0x101E`) and the Sigenergy OEM map over FC04 (primary side, `0x150A`
measurements in SI units except power/reactive power in kW/kvar). Both sides derive from
the same ND45 reading; the classic map divides current/power/energy by the configured CT
ratio (`dtsu.identity.ir_at`) while the Sigen map does not (already primary). It also
serves the FC03 identity string `Sigen Sensor TPX-CH` at `0xF100` and the observed
`0x00001500` handshake at `0xF114`. Apparent power is read directly from ND45
registers `60`/`84`/`108` (L1/L2/L3) and `132` (L123 sum), then served on both maps
(classic `0x2022`, Sigen FC04 `0x152C`).

The Sigen FC04 energy map reproduces the physical TPX-CH behavior rather than
assuming a uniform `+0x800` copy of the generic DTSU666 map. `0x1800` is
combined active energy, `0x180A` is exported reactive energy (Q-), and
`0x1814` is imported reactive energy (Q+). `0x181E` is precise active import;
`0x1828` is precise total active export, with L1/L2/L3 export at
`0x182A`/`0x182C`/`0x182E`. The classic FC03 energy aliases are CT-side values,
while the directional `net_*` fields repeat their corresponding import/export
totals as on the physical meter.

Full register layouts, address by address:

- [`docs/register-map.md`](docs/register-map.md) — the ND45 bridge, verified against
  a live-meter scan.
- [`docs/smartlogger-dtsu-map.md`](docs/smartlogger-dtsu-map.md) — the Huawei
  SmartLogger bridge: which Huawei register feeds which canonical point, how the
  gaps are filled, and how each one lands in the DTSU666 maps. Its tables are
  generated from `config/registers.json`, so they cannot drift.

## Install (reComputer R1000, Ubuntu)

Deployed installs live under `/persistence/app/<version-dir>`, with
`/persistence/app/current_modbus_converter` kept as a symlink to whichever
checked-out version is live — the systemd units always point at the symlink, so
an upgrade is "check out the new version, repoint the symlink, restart".
Full procedure, including upgrade/rollback: `/persistence/nd45-dtsu666-DEPLOY.md`.

```bash
sudo mkdir -p /persistence/app
cd /persistence/app
git clone https://github.com/korneliuszm/nd45_dtsu666_converter.git nd45_hsm_dtsu666
sudo chown -R $USER:$USER nd45_hsm_dtsu666
sudo ln -s /persistence/app/nd45_hsm_dtsu666 /persistence/app/current_modbus_converter
cd /persistence/app/current_modbus_converter
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

Edit `config/config.json`. Everything runtime lives under `bridges`, one entry per
bridge; in the `nd45` entry set `source.host`, and under `dtsu` pick the output
`transport`:
- `"rtu"`: fill in `dtsu.rtu` — the RS-485 device `port` (`/dev/ttyAMA2` etc.),
  `baudrate`, `parity`, `stopbits`.
- `"tcp"`: fill in `dtsu.tcp` — `host` to bind (`0.0.0.0` for all interfaces) and `port`
  (502 is the Modbus TCP default).

`dtsu.slave_id` applies to both transports (RS-485 slave address for RTU, unit id in
the MBAP header for TCP) and must match what Sigenergy is configured to poll.

The optional `prometheus` section controls the metrics endpoint (see below); omit it
to accept the defaults.

## Independent bridges

A **bridge** is a complete source → DTSU666 translator with its own upstream client,
canonical store, served datastore, freshness gate and output transport. The intended
deployment here is two, each running as its own systemd service:

| | bridge `nd45` | bridge `smartlogger` |
|---|---|---|
| source | Lumel ND45 (Modbus TCP, float32) | Huawei SmartLogger (Modbus TCP) |
| output | RS-485 `/dev/ttyAMA2` | RS-485 `/dev/ttyAMA4` |
| what Sigenergy sees | grid-tie balance | PV farm production |
| `safety.max_data_age_s` | 3.0s (polled every 0.3s) | 30.0s (polled every 5s) |

Two levels of isolation, and both matter:

- **Inside a process** — bridges share nothing but the event loop, so one source
  going dark silences that bridge's RS-485 alone. The Sigenergy on that bus sees a
  meter timeout and enters its own safe mode while the other bus keeps serving.
- **Across processes** — running each bridge as its own service (`run --bridge`)
  means a crash, an OOM, a bad deploy or a routine `systemctl restart` on one cannot
  touch the other either. See "Run as a service".

One process can still run every bridge (`run` with no `--bridge`), which is what
`monitor` and the debug modes do.

### Configuration

**Every** bridge is one entry in `bridges`, and all entries have the same shape —
`name`, `enabled`, `source`, `dtsu`, `safety`, `metrics_port`. The ND45 bridge and the
SmartLogger bridge differ only in their values, so the two are read side by side:

```json
"bridges": [
  {
    "name": "nd45",
    "enabled": true,
    "source": {
      "type": "nd45", "host": "192.168.22.109", "port": 502, "unit_id": 1,
      "register_map": "nd45_source",
      "poll_interval_s": 0.3, "timeout_s": 1.0, "stall_timeout_s": 30.0
    },
    "dtsu": {
      "transport": "rtu", "slave_id": 10,
      "identity": {"rev": 103, "ucode": 701, "ir_at": 200, "ur_at": 10},
      "rtu": {"port": "/dev/ttyAMA2", "baudrate": 9600, "parity": "N", "stopbits": 1}
    },
    "safety": {"max_data_age_s": 3.0, "check_interval_s": 0.5},
    "metrics_port": 8081
  },
  {
    "name": "smartlogger",
    "enabled": false,
    "source": {
      "type": "huawei", "host": "192.168.22.120", "port": 502, "unit_id": 0,
      "register_map": "huawei_plant_source",
      "poll_interval_s": 5.0, "timeout_s": 6.0, "stall_timeout_s": 60.0
    },
    "dtsu": {
      "transport": "rtu", "slave_id": 10,
      "identity": {"rev": 103, "ucode": 701, "ir_at": 200, "ur_at": 10},
      "rtu": {"port": "/dev/ttyAMA4", "baudrate": 9600, "parity": "N", "stopbits": 1}
    },
    "safety": {"max_data_age_s": 30.0, "check_interval_s": 0.5},
    "metrics_port": 8082
  }
]
```

Outside `bridges` there are only the two process-wide sections, `prometheus` and
`static_debug`.

The `smartlogger` entry ships with `enabled: false` — fill in `source.host` and flip
it to turn the second bridge on. `slave_id` may repeat across bridges (the RS-485
buses are electrically independent); the serial port may not. Config load fails fast
on:

- two bridges on one serial port (or one TCP bind) — pymodbus 3.6.9's `listen()`
  swallows the resulting `OSError`, so the loser would hang silently instead of
  failing
- `safety.max_data_age_s` below twice `source.poll_interval_s` — that parks a bridge
  in permanent fail-safe, which in the field looks exactly like a dead source
- a duplicate bridge name, or a metrics port colliding with any bridge
- a config with no bridges at all, or with every bridge disabled

The older layout, in which top-level `nd45` / `dtsu` / `safety` /
`primary_metrics_port` keys described the single bridge, is still accepted, so a
config file written before multi-bridge support keeps loading; those keys become
the first bridge, named `nd45`. New files should use `bridges` throughout.

### Source types and register maps

`source.type` selects the decoder, `source.register_map` the section of
`config/registers.json` it decodes:

| `register_map` | source | canonical points read directly |
|---|---|---|
| `nd45_source` | ND45, float32 | 36 of 36 |
| `huawei_plant_source` | SmartLogger's own plant registers (logic device 0) | 10 of 36 |
| `huawei_meter_source` | a power meter behind the SmartLogger (its RS-485 address) | 20 of 36 |

Neither Huawei set covers the whole canonical model, so those maps carry declarative
`derive` rules (`split_equal`, `phase_from_line`, `hypot`, `ratio_split`,
`pf_from_p_s`, `copy`, `constant`) that fill the rest — editable on site without
touching code, like the register maps themselves. **Grid frequency appears nowhere in
the SmartLogger manual**, so it is a `constant` (50.0 Hz). Every point uses plain
canonical names; the `bridge` metric label is what separates the two.

Note that `huawei_plant_source` reports PV *production*, not the grid *balance* a
Sigenergy Power Sensor normally measures. That is the intent here — bridge B is a
separate meter reporting the farm — but if the Sigenergy on that bus is meant to
*regulate* rather than just report, the semantically correct source is a meter at the
grid-tie point (`"register_map": "huawei_meter_source"` plus that meter's RS-485
address in `unit_id`).

### Observability

Per-bridge series carry a `bridge` label:
`nd45_dtsu666_bridge_data_age_seconds{bridge="smartlogger"}`,
`_bridge_dtsu_server_up`, `_bridge_polls_total`, `_bridge_poller_restarts_total`,
`_bridge_canonical_value{point=...}`. The original unlabelled `nd45_*` / `dtsu_*`
families are still emitted as aliases for the first bridge, so existing dashboards
keep working. `/healthz` returns 200 only when **every** bridge is fresh and names
the stale ones. `monitor_nd45` / `monitor_hsm` show one screen per bridge (see below).

### Hung-poller recovery

A poll loop that stops making progress for `source.stall_timeout_s` is torn down and
rebuilt in place (fresh client, reconnect, restart) by `app.supervise_poller`, per
bridge. That is the cheap first attempt; the systemd watchdog is the backstop if the
rebuild itself loops. See "Run as a service" for how the two are ordered.

An unreachable source is *not* a stall: the poller keeps cycling through its error
path, the data goes stale, and `supervise_server` silences that bridge — no rebuild.

The trade-off to accept: a hung poller no longer has an external safety net. Alert on
`nd45_dtsu666_bridge_poller_restarts_total` — a rising counter means recovery is
looping rather than fixing anything.

Design notes and the full register-coverage matrix:
[`docs/superpowers/specs/2026-07-30-huawei-smartlogger-source-design.md`](docs/superpowers/specs/2026-07-30-huawei-smartlogger-source-design.md).

## Prometheus metrics

Every mode (`run`, `monitor`, `rtudebug`, `static`) serves a read-only metrics
endpoint, so the bridge can be checked from Grafana instead of over SSH:

```bash
curl -s http://<device>:8081/metrics
curl -s http://<device>:8081/healthz    # "ok" while ND45 data is fresh, 503 when stale
```

Configured in `config/config.json` (all fields optional):

```json
"prometheus": {"enabled": true, "host": "0.0.0.0", "port": 8081, "include_registers": true}
```

- `host` — `0.0.0.0` to allow scraping from the LAN, `127.0.0.1` to require an SSH tunnel.
- `port` — 8081 as shipped (the code default is 9090, which is also the default port
  of the Prometheus server itself — harmless when Prometheus runs on another machine,
  but worth avoiding if you ever put Prometheus on the reComputer). A port colliding
  with `dtsu.tcp.port` is rejected at startup. Open it in `ufw` if the firewall is
  enabled.
- `include_registers` — set `false` to drop the per-point value gauges and keep only
  the health metrics.

`--metrics-port N` and `--no-metrics` override the config, e.g. when running `monitor`
alongside the service during bring-up:

```bash
python -m nd45_dtsu666 --metrics-port 8090 monitor
```

All names are prefixed `nd45_dtsu666_`:

| Metric | Meaning |
|---|---|
| `build_info{version,mode,transport,dtsu_slave_id}` | `mode="static"` means the served values are synthetic |
| `nd45_connected` | 1 while the ND45 Modbus TCP link is up |
| `nd45_data_age_seconds` / `nd45_data_fresh` | data age and whether the fail-safe threshold is met |
| `nd45_polls_total{result="ok"\|"error"}` | poll attempts by outcome |
| `nd45_consecutive_poll_failures` | failures since the last good poll |
| `nd45_last_poll_error_info{type}` | exception type of the last failure (message stays in the journal) |
| `watchdog_heartbeat_age_seconds` | poller liveness as systemd's watchdog sees it |
| `dtsu_server_up` | 1 while the output transport is actually serving (0 = fail-safe or port problem) |
| `dtsu_requests_total`, `dtsu_request_rate_per_second`, `dtsu_last_request_age_seconds` | whether Sigenergy is polling at all |
| `dtsu_block_reads_total{fc,addr,count}` | which register blocks it asks for |
| `bridge_dtsu_bus_bytes_total{bridge}` | bytes received on that bridge's RS-485 port, whatever they address |
| `bridge_dtsu_bus_frames_total{bridge,slave_id}` | valid request frames on the bus, by the slave id they address |
| `canonical_value{point}` | latest ND45 value in SI units |
| `dtsu_register_value{map,point,fc,addr}` | value currently encoded in each DTSU output register |
| `dtsu_register_read_age_seconds{map,point,fc,addr}` | seconds since Sigenergy last read that register (`+Inf` = never) |

Useful alert expressions:

```promql
nd45_dtsu666_nd45_data_fresh == 0                      # ND45 stale -> output silenced
nd45_dtsu666_dtsu_server_up == 0                       # output server down
rate(nd45_dtsu666_dtsu_requests_total[5m]) == 0        # Sigenergy stopped asking
# something polls the bus but never us -> wrong Modbus address on that bridge
rate(nd45_dtsu666_bridge_dtsu_bus_frames_total[5m]) > 0
  unless on(bridge) rate(nd45_dtsu666_bridge_dtsu_requests_total[5m]) > 0
increase(nd45_dtsu666_nd45_polls_total{result="error"}[5m]) > 0
```

The endpoint starts before the ND45 connection is attempted, so it is reachable
during a startup outage — `nd45_connected 0` with `data_age +Inf` is the expected
picture then. It is read-only and unauthenticated; keep it on a trusted network.

Design notes: `docs/superpowers/specs/2026-07-27-prometheus-metrics-design.md`.

## Bench test before connecting Sigenergy
Run the configured transport with synthetic data and read it with mbpoll.

RTU (`dtsu.transport: "rtu"`):
```bash
python -m nd45_dtsu666 selftest
# in another shell (RTU master), read holding registers as float from address 8192:
mbpoll -m rtu -a 1 -b 9600 -P none -t 4:float -r 8193 -c 4 /dev/ttyUSB0
```

TCP (`dtsu.transport: "tcp"`):
```bash
python -m nd45_dtsu666 selftest
# in another shell (TCP master), read holding registers as float from address 8192:
mbpoll -m tcp -a 1 -t 4:float -r 8193 -c 4 127.0.0.1 -p 502
```
(Note mbpoll `-r` is 1-based; register 8192 → `-r 8193`. Confirm word order matches.)

## Diagnostics
```bash
python -m nd45_dtsu666 diag                  # live table: canonical SI, DTSU addr/raw, age, status
python -m nd45_dtsu666 diag --interval 0.3   # sample as fast as the bridge itself does
```

`diag` talks to the source directly and serves nothing, so it isolates the meter
from the rest of the bridge. Under the table it prints the spread of each watched
point across the session:

```
spread over 26 sample(s) at 0.3s (8s of history)
point                 last           min           max     peak-peak  sign flips
p_total           -960.228     -3219.326      1607.461      4826.787          10
u_l1               230.000       230.000       230.000         0.000           0
```

**Reading a value that looks unstable in `monitor` but steady in `diag`:** the
two differ in sample rate, not in arithmetic — `diag` polls every 1s by default
while the bridge polls at `source.poll_interval_s` (0.3s for the ND45). Re-run
`diag --interval 0.3`; if the spread and the sign flips appear, the meter really
does deliver that at 3 Hz and the monitor is being honest. Raising
`poll_interval_s` toward 1s then smooths what Sigenergy sees (`max_data_age_s`
must stay at least twice it — config load enforces that).

Do this with the bridge's service **stopped**. `diag` and `monitor` each open
their own Modbus TCP connection, so running one alongside `nd45-dtsu666@nd45`
puts two masters on a meter that is being polled three times a second — which is
itself enough to make readings erratic on a small analyser. `monitor` also
cannot open the RS-485 port while the service holds it (see the `bus traffic:`
line).

## Interactive monitor (commissioning)

```bash
python -m nd45_dtsu666 monitor_nd45    # the ND45 bridge's screen
python -m nd45_dtsu666 monitor_hsm     # the Huawei SmartLogger bridge's screen
python -m nd45_dtsu666 monitor         # one screen per bridge, stacked
python -m nd45_dtsu666 --bridge smartlogger monitor   # by name
```

**Every configured bridge runs in all of these** — the flag only chooses what is on
screen, so watching one bridge never changes the other's timing or leaves its output
unserved. `monitor_nd45` / `monitor_hsm` resolve by *source type*, so they still find
the right bridge if you renamed it in `config.json`.

Each screen answers the two questions bring-up actually asks, per bridge:

```
 bridge 'smartlogger'   Huawei SmartLogger -> DTSU666   state: SERVING
------------------------------------------------------------------------
 SOURCE  Huawei SmartLogger  192.168.22.120:502 unit 0
   link: CONNECTED  data age: 2.40s / 30.0s   poll: OK (every 5s)
   polls: 721 ok / 0 err   consecutive fails: 0   last ok: 2.4s ago
   Phase      U [V]    I [A]       P [W]     Q [var]      PF
   L1         231.0   100.00    411500.0    -16666.7   0.999
   ...
   TOTAL                       1234500.0    -50000.0   0.999   f=50.00 Hz
   Direction: IMPORT (P>0)      E_imp=0.0  E_exp=12345.6 kWh
   E_daily=456.7   P_dc=1250000.0
------------------------------------------------------------------------
 OUTPUT  DTSU666 slave 10  on /dev/ttyAMA4 @9600 8N1
   server: UP                  starts: 1  stops: 0
   Sigenergy reads: 1203    rate: 1.1/s    last seen: 0.9s ago
   blocks read:  FC03 @8192 x40 (601)   FC04 @5386 x50 (602)
   recent:  @8192x40  @5386x50
------------------------------------------------------------------------
```

- **SOURCE** — is the upstream link up (`link: CONNECTED / DOWN / UNKNOWN`), how old
  the data is against *this bridge's own* threshold, poll counters, last error type,
  and `poller restarts` (a rising count means stall recovery is looping). Then the
  decoded per-phase values. `E_daily` / `P_dc` appear only for a SmartLogger source.
- **OUTPUT** — which port this bridge serves, whether its server is UP or silenced by
  the fail-safe, and whether **Sigenergy is actually reading on that port**: request
  count, rate, last seen, and which register blocks it asked for. `(none yet --
  Sigenergy has not polled this port)` is the answer you are looking for when
  commissioning the second bus.

The served DTSU register dump is deliberately **not** in the monitor — it is long and
pushed the link and activity lines off screen. Use `rtudebug` to trace register
blocks, or the Prometheus `*_bridge_dtsu_register_value` series.

## Debug: which registers does Sigenergy read?
For raw protocol debugging (no dashboard), `rtudebug` runs the same live bridge but
logs one line per read request from Sigenergy — function code, address (decimal + hex),
word count, and the DTSU register name(s) that block touches — after printing the full
`addr → name` map once at startup. It reuses the same non-invasive read hook as
`monitor`, so it does not affect the standard `run` service. Redirect to a file to keep a
capture:
```bash
python -m nd45_dtsu666 rtudebug > rtu_debug.log 2>&1   # Ctrl-C to quit
```

## Static debug values for Sigenergy

Use `static` when rapidly changing ND45 measurements make commissioning hard.
This mode does not connect to the ND45. It continuously serves the fixed values
from `static_debug.values` in `config/config.json` through the same classic
FC03 and Sigen FC04 maps as the live bridge:

```bash
python -m nd45_dtsu666 static
```

The dashboard is labeled `STATIC DEBUG` and still shows the blocks requested by
Sigenergy. Edit the JSON values before startup; omitted measurements are served
as zero, except omitted phase apparent power (`s_l1/s_l2/s_l3`) is calculated
from the configured U and I, and omitted `s_total` is their sum. Explicit `s_*`
values are served unchanged. Values must be finite JSON numbers, and unknown
names stop startup so spelling mistakes cannot silently produce zeros.

The exact independently configurable `static_debug.values` keys are:

```text
u_l1, u_l2, u_l3, u_l12, u_l23, u_l31
i_l1, i_l2, i_l3
p_l1, p_l2, p_l3, p_total
q_l1, q_l2, q_l3, q_total
s_l1, s_l2, s_l3, s_total
pf_l1, pf_l2, pf_l3, pf_total
freq
imp_energy_total, imp_energy_l1, imp_energy_l2, imp_energy_l3
exp_energy_total, exp_energy_l1, exp_energy_l2, exp_energy_l3
reactive_imp_energy_total, reactive_exp_energy_total
```

No other keys are accepted. `active_energy_total`, `net_imp_energy_total`, and
`net_exp_energy_total` are derived from these independent values; the other
energy fields map directly to their physical register counterparts.

The checked-in image represents about 7 kWh import and 3 kWh export, including
0.8/1.0/1.0 kWh L1/L2/L3 export; Q+ is 1.2 kvarh and Q- is 2.8 kvarh. Active
power and PF are negative. `active_energy_total`, both `net_*` values,
CT-side values, and coarse aliases are derived automatically.

Only one process can own an RTU serial port. Stop that bridge's service (or
`monitor`) before starting static mode -- `static` targets one bridge at a
time (`--bridge`, default the first configured):

```bash
sudo systemctl stop nd45-dtsu666@nd45
python -m nd45_dtsu666 static
# Ctrl-C when finished
sudo systemctl start nd45-dtsu666@nd45
```

Static mode is strictly opt-in: the `run`, `monitor`, `rtudebug`, and `selftest`
commands continue to use their existing data sources.

## Run as a service

**One systemd instance per bridge.** Each bridge already owns its store, datastore,
fail-safe and RS-485 port inside the process, but the process itself was still a
shared fate: a crash, an OOM, a bad deploy or a plain `systemctl restart` silenced
*both* meters. Separate services remove that last coupling — verified by SIGKILLing
one service and watching the other keep serving.

```bash
sudo cp systemd/nd45-dtsu666@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nd45-dtsu666@nd45
sudo systemctl enable --now nd45-dtsu666@smartlogger

systemctl status nd45-dtsu666@smartlogger
journalctl -u nd45-dtsu666@smartlogger -f
sudo systemctl restart nd45-dtsu666@smartlogger   # grid-tie meter untouched
```

`%i` is the bridge `name` from `config.json`, passed through as `run --bridge %i`.

**Both instances read the same `config/config.json`, deliberately.** The validators
that reject two bridges sharing a serial port (or colliding TCP/metrics ports) only
work when one configuration sees every bridge. Splitting into per-service config
files would lose that check — and pymodbus 3.6.9 swallows the resulting bind error,
so the losing service would hang in silence instead of failing.

Because two processes cannot share a Prometheus port, each bridge sets its own
`bridges[].metrics_port` (8081 and 8082 as shipped). Running everything in one
process still uses the shared `prometheus.port`.

### Single-process alternative

`systemd/nd45-dtsu666.service` runs **every** enabled bridge in one service
(`run` with no `--bridge`). Fine for a single-bridge install or for bring-up, where
one journal is easier to follow — but for the two-bridge deployment prefer the
per-instance unit: besides a restart dropping both buses, two Modbus TCP pollers
sharing one event loop have been observed to corrupt each other's reads outright.
See `/persistence/nd45-dtsu666-DEPLOY.md` §0 before running more than one bridge
in a single process.

### The watchdog

`WatchdogSec=90` tracks **this instance's poll-loop progress**. A stalled loop is
first rebuilt in place by `app.supervise_poller` (`source.stall_timeout_s`, 60s),
and only if that recovery keeps looping does systemd restart the service — which is
now safe, because the restart no longer touches the sibling bridge. An unreachable
source is *not* a stall: the poller keeps cycling, the data goes stale, and the
freshness gate silences that bridge's output on its own.

## On-site verification checklist (before leaving unattended)
1. **Sign convention** — with known import/export, confirm Sigenergy sees correct grid
   direction. If reversed, set `sign: -1` on the `p_*` target points in `registers.json`.
2. **Phase order** — confirm L1/L2/L3 == A/B/C. Swap `from` keys if needed.
3. **Scaling** — voltages/currents/power read plausibly on Sigenergy.
4. **Word/byte order** — if values look garbled, flip `word_order`/`byte_order` in the map.
5. **Slave ID / transport params** — match what Sigenergy polls: slave/unit id always;
   baud/parity/stopbits for RTU, or host/port for TCP.
6. **Fail-safe** — pull the ND45 network cable; confirm the output server goes silent
   (`journalctl` shows "fail-safe") and Sigenergy enters its safe mode.
7. **RS-485 direction** (RTU transport only) — verify the reComputer transceiver
   auto-toggles direction, or configure pyserial RS-485 mode if the master sees
   no/garbled replies. Not applicable when `dtsu.transport` is `"tcp"`.
8. **Identity/config registers (0x0000-0x002E)** — values are set from `dtsu.identity` in
   `config/config.json` (defaults: direct-connect 3P4W meter, CT ratio 1:1 — `net=0`,
   `ir_at=ur_at=10`). `ir_at` is **not** cosmetic: it is the actual CT ratio the classic
   DTSU666 map (FC03) divides current/power/energy by, since that map is secondary-side
   while the ND45 source (already converted internally by the ND45) and the Sigen OEM map
   (FC04) are primary-side — get it wrong and every classic-map value is off by that
   factor. To match a specific real meter, edit `dtsu.identity` by hand, e.g.:
   ```json
   "identity": {"rev": 103, "ucode": 701, "ir_at": 200, "ur_at": 10}
   ```
   Fields omitted keep their default. See `DtsuIdentityConf` in `config.py` for the full
   field list and `_IDENTITY_REGISTER_ADDRS` in `dtsu_server.py` for the register mapping
   (background: `docs/superpowers/specs/2026-07-23-dtsu-sigen-ct-ratio-design.md`).
9. **Metrics endpoint** — `curl http://<device>:8081/metrics` from the machine that
   will scrape it (not just from the device itself, so the firewall is covered too).
   Confirm `nd45_dtsu666_nd45_data_fresh 1`, `nd45_dtsu666_dtsu_server_up 1`, and a
   rising `nd45_dtsu666_dtsu_requests_total`. Check that
   `nd45_dtsu666_dtsu_register_read_age_seconds` is small for the registers Sigenergy
   actually polls — a stuck `+Inf` on a register you expect it to read means the map
   or the function code is wrong.
10. **Sigen OEM registers** — confirm the storage reads FC04 `0x151C` for total active
   power (in kW, not W) and periodically reads FC03 `0xF114` for the identity handshake.
   The configured FC04 current and per-phase power positions follow the confirmed
   `-0x0AF6` block offset. The reverse-flow scan also confirms the physical energy image:
   combined active energy, distinct Q+/Q- counters, and non-zero total and per-phase
   export energy. Continue to capture the installation under load to verify its sign,
   phase order, and scaling against the connected ND45.
11. **Second bridge** (only if a `bridges` entry is enabled) — checks the test suite
    cannot cover:
    - **`/dev/ttyAMA4` exists.** `ls -l /dev/ttyAMA*` on the device. If the second
      UART is not enabled in the device tree it will not be there, and that is a
      system-level fix (overlay), not a code one. Config load rejects two bridges
      sharing a port, but it cannot conjure a port that does not exist.
    - **RS-485 direction on the second port** — same check as item 7 for `ttyAMA2`.
    - **If Sigenergy never appears**, read the OUTPUT panel's `bus traffic:` line
      (or `bridge_dtsu_bus_*`), which counts raw frames *before* pymodbus filters
      by slave id. It separates the three faults that otherwise look identical:
      `none` = nothing is transmitting (wiring, A/B swap, direction control, or the
      storage is not configured to poll); `frames for: slave N` with reads at 0 =
      the storage polls a different Modbus address than `dtsu.slave_id`; bytes but
      `no valid Modbus frame decoded` = baudrate/parity/framing mismatch. A missing
      or busy port is different again — the journal then repeats "DTSU server never
      opened its transport".
    - **Address base of the SmartLogger.** Huawei documents "40525"; some Modbus
      clients need `40524` (0- vs 1-based). Probe with `mbpoll` *before* enabling the
      bridge, and correct via `address_offset` on the source in `registers.json`.
    - **CT ratio of bridge B** (`ir_at`, copied from bridge A). Bridge B represents a
      different meter; at MW-scale production confirm the Sigenergy on that bus reads
      plausible values on the FC03 map, which divides by `ir_at`.
    - **Refresh rate.** Watch `bridge_data_age_seconds{bridge="smartlogger"}`. If it
      regularly exceeds `safety.max_data_age_s`, raise **that bridge's** threshold —
      never the ND45 bridge's.
    - **Production sign.** Confirm `p_total` on bridge B is positive while generating.
    - **Plant current resolution.** Registers 40572-74 have Gain 1 (I16): a 1 A step
      and overflow past 32767 A. On a larger farm treat those phase currents as
      indicative; only `huawei_meter_source` gives trustworthy values.
    Then pull each source cable in turn and confirm the isolation: killing the
    SmartLogger must leave `bridge_dtsu_server_up{bridge="nd45"}` at 1 with port A
    still answering, and killing the ND45 must leave bridge B serving. `/healthz`
    names whichever bridge is stale.
