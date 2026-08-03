# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **temporary Modbus protocol bridge**: one process running one or more independent **bridges**, each polling an upstream device over **Modbus TCP** (as client) and re-serving it as a CHINT **DTSU666** power meter (as server), so a **Sigenergy** battery system can read it as its "Power Sensor". The output transport is config-selectable per bridge: **Modbus RTU / RS-485** or **Modbus TCP**. It is a bridging solution meant to run for a few months until a physical DTSU666 meter arrives — favor short, safe, working changes over long-term architecture.

This deployment runs **two bridges**: `nd45` (Lumel ND45 → `/dev/ttyAMA2`, grid-tie balance) and `smartlogger` (Huawei SmartLogger → `/dev/ttyAMA3`, PV farm production). See `docs/superpowers/specs/2026-07-30-huawei-smartlogger-source-design.md`.

It is **not** a 1:1 gateway: it translates between two different register maps via an intermediate canonical model in SI units.

## Commands

Development is on Windows (PowerShell + Git Bash); the deploy target is Ubuntu on a Seeed reComputer R1000. Use the project venv interpreter directly rather than activating:

- **Windows dev:** `.venv\Scripts\python.exe -m <tool>`
- **Linux target:** `.venv/bin/python -m <tool>`

```bash
python -m venv .venv && pip install -e ".[dev]"   # setup
python -m pytest -q                                # all tests
python -m pytest tests/test_codec.py::test_roundtrip_all_orders -v   # single test
python -m ruff check .                             # lint (line-length 100)
python -m nd45_dtsu666 run                         # run every enabled bridge
python -m nd45_dtsu666 monitor                     # bridges + a live dashboard panel each
python -m nd45_dtsu666 monitor_nd45                # all bridges run; show the ND45 one
python -m nd45_dtsu666 monitor_hsm                 # all bridges run; show the SmartLogger one
python -m nd45_dtsu666 rtudebug                     # run all, trace register reads on one
python -m nd45_dtsu666 diag                        # poll one bridge's source, print the table
python -m nd45_dtsu666 selftest                    # serve synthetic DTSU data for mbpoll bench test
python -m nd45_dtsu666 --bridge smartlogger diag   # single-bridge modes take --bridge
curl -s localhost:9090/metrics                     # Prometheus scrape (served by run/monitor/rtudebug/static)
```

CI (`.github/workflows/ci.yml`) runs `ruff check` + `pytest` on Python 3.10/3.11/3.12. There is no separate build step — it's a pure Python package.

## Architecture

Single-process **asyncio**, one event loop, no locks. The core data flow:

```
per bridge, sharing only the event loop and the metrics endpoint:

source (TCP slave) --FC03--> poller --decode--> CanonicalStore --> dtsu datastore
                                                     |             (encode -> DTSU registers)
                                        supervise_server (this bridge's freshness gate)
                                                     |
Sigenergy (RTU or TCP master) --FC03--> DTSU output server (serves instantly from the
                                        datastore, never waits on a source poll)
```

- `app.build_pipeline()` wires **every enabled bridge** and is shared by `run` (`run_app`), `monitor`, and `rtudebug`. It returns a `Pipeline` holding a list of `BridgeRuntime` (store, context, client, client_factory, heartbeat, stat holders — nothing shared between bridges) plus the coroutines and a `MetricsSource`. `Pipeline.store`/`.context`/`.client` are back-compat accessors for the **first** bridge, which is what the single-bridge modes use. When editing the wiring, change `build_pipeline`, not the callers.
- **Bridges are the unit of isolation, and that is load-bearing.** Read `AppConfig.bridge_specs`, never the raw `nd45`/`dtsu`/`safety` fields — those are kept only so pre-existing config files still load, and `bridge_specs` assembles them into the first bridge. Each bridge has its **own** `safety.max_data_age_s` (3.0s at 0.3s polling for ND45; 30.0s at 5s polling for the SmartLogger, which aggregates over RS485 and cannot meet 3s). Config load rejects two bridges sharing a serial port — pymodbus 3.6.9's `listen()` swallows the `OSError`, so the loser would hang silently — and rejects `max_data_age_s < 2 x poll_interval_s`, which would park a bridge in permanent fail-safe. `slave_id` *may* repeat; the buses are electrically independent. `tests/test_bridge_isolation.py` is the file that guards all of this.
- **A hung poller is recovered in-process, per bridge** (`app.supervise_poller`): no progress for `source.stall_timeout_s` means cancel the task, close the client, rebuild it via `client_factory`, reconnect, restart. A systemd restart would take every bridge down at once, so `watchdog_loop` now watches **event-loop liveness** (`app.loop_ticker`) rather than poller progress. Important distinction: an *unreachable* source is not a stall — `run_poller` keeps cycling through its error path and touching the heartbeat, so nothing is rebuilt and only the freshness gate reacts. The cost of this design is that stall recovery has no external safety net, which is why `nd45_dtsu666_bridge_poller_restarts_total` is the metric to alert on.
- `codec.py` is a **`struct`-based** float32 ↔ register codec, plus a sized-integer half (`u16/i16/u32/i32/u64/i64`) used only by the Huawei source. Do **not** use pymodbus `BinaryPayloadBuilder`/`Decoder` (removed in newer pymodbus, and the version is pinned to `>=3.6,<3.7`). Huawei's documented "Gain" is a divisor but needs no new concept — it folds into the map's existing `scale` multiplier.
- `canonical.py` (`CanonicalStore`, `HealthGate`) holds the latest SI values + a timestamp and is the single source of truth. `dtsu_server.update_datastore` mirrors those values into the pymodbus datastore. It also holds `compute_derived` (canonical-model logic every source must run; re-exported from `nd45_poller` for existing importers) and `apply_derive`.
- **Huawei SmartLogger source** (`huawei_poller.py`). `poll_once` is signature-compatible with `nd45_poller.poll_once`, which is what lets `run_poller` drive either via `poll_once_fn`; `app._POLL_ONCE` is the only place a source type is dispatched on. It uses **sized integers** (Huawei's documented "Gain" is a divisor that folds into the map's existing `scale` multiplier) and **config-driven read blocks** (`SourceSide.read_groups`) rather than a module constant. Coverage is partial by necessity — no SmartLogger register set covers the full canonical model, and grid frequency appears **nowhere** in its manual — so the maps carry declarative `derive` rules (`canonical.apply_derive`). Two things to know: an invalid channel is zeroed rather than rejecting the whole sample (losing a 5s-refresh poll over one disconnected inverter is too costly; a genuinely unreachable source still raises from `read_groups`), and it **does** call `compute_derived`, because its bridge owns a full canonical model and must fill `active_energy_total`/`net_*`. Point names are plain canonical names — the `bridge` metric label separates the two bridges, not a prefix.
- **Fail-safe** (`dtsu_server.supervise_server`): one instance **per bridge**. When a bridge's data is older than *its own* `safety.max_data_age_s`, *that* bridge's output server is **stopped** (goes silent) so the Sigenergy on that bus detects a timeout and enters its own safe mode; it restarts automatically when data returns. A sibling bridge is unaffected — verified live in both directions.
- **Watchdog** (`watchdog.py`): the systemd unit's `WatchdogSec=` (read from the `WATCHDOG_USEC` env var, no duplication in `config.json`) drives a heartbeat fed by `app.loop_ticker`, i.e. **event-loop liveness**. It used to track ND45-poller progress; that moved to the per-bridge `supervise_poller` once a restart started taking sibling bridges down with it. Only a wedged loop or a dead process escalates to systemd. See `docs/superpowers/specs/2026-07-06-systemd-watchdog-design.md` for the original design and `2026-07-30-huawei-smartlogger-source-design.md` for why it changed.
- **Metrics** (`metrics.py`): a read-only Prometheus endpoint on `asyncio.start_server` (no `prometheus_client` -- its `start_http_server` uses a thread, and this process is one loop with no locks). It reads existing state at scrape time (`CanonicalStore`, `RtuActivity`, `Heartbeat`) plus two new holders, `PollStats` (fed by `build_pipeline`'s `on_update`/`on_error`; the poller is untouched) and `ServerStatus` (set by `supervise_server`'s optional `status=`, since server state is otherwise loop-local and cannot be re-derived from the freshness gate). Two things are easy to get wrong: the task is created **before** `connect_with_retry` in every runner -- `pipe.coros` only start after the ND45 connects, and an endpoint that is down during an ND45 outage is useless -- and `build_pipeline` now creates an `RtuActivity` itself when the endpoint is enabled, so `run` gets read stats too. See `docs/superpowers/specs/2026-07-27-prometheus-metrics-design.md`.
- `monitor.py` renders **one screen per bridge** from a pure `render_bridge_monitor(BridgeSnapshot)`: upstream link state + decoded values (SOURCE), and server state + Sigenergy read activity **on that bridge's own port** (OUTPUT). The served-register dump was removed on purpose — it pushed the link and activity lines off screen, and `rtudebug` already traces register blocks. `monitor_nd45`/`monitor_hsm` resolve a bridge by *source type* (`select_bridge_by_source`), so a renamed bridge still works; every bridge runs regardless of which one is displayed. Read requests from Sigenergy (over either transport) are captured via `RecordingSlaveContext` (a `ModbusSlaveContext` subclass logging every `getValues` into an `RtuActivity` tracker); passing `activity=` to `build_pipeline` now gives **every** bridge its own recorder, not just the first — otherwise a sibling panel could not answer "is Sigenergy polling this port?" with the metrics endpoint switched off.

## Register maps and translation (the crux)

- The maps live in **`config/registers.json`** (seeded from the device PDFs) and are edited **without touching code**. ND45 addresses are **decimal**; DTSU666 addresses are **decimal converted from the manual's hex**. `config/config.json` holds runtime params per bridge (source IP/unit/intervals, output transport + its params, slave id, `max_data_age_s`).
- **Source sections vs output maps.** `registers.json` holds one source section per device kind (`nd45_source`, `huawei_plant_source`, `huawei_meter_source`), selected by a bridge's `source.register_map`; the four output maps (`dtsu_target`, `dtsu_sigen_ext_target`, `dtsu_sigen_ext_energy`, `dtsu_sigen_identity`) are **shared by every bridge** — they all emulate the same meter. What differentiates a bridge's output is only its `dtsu.identity.ir_at`, passed to `update_datastore` as the runtime `ct_ratio`. Read them via `RegisterMap.targets` / `RegisterMap.source_by_name`.
- **Transform semantics** (implemented in `codec.decode_point`/`encode_point`, must stay exact):
  - ND45 → canonical: `SI = (raw_float * scale * sign) + offset`
  - canonical → DTSU register: `register_float = (SI * sign * scale) + offset`
  - DTSU scales are e.g. V×10, A×1000, W×10, PF×1000, Hz×100; energy is direct kWh. `sign ∈ {+1,-1}` flips import/export.
- Both sides default to big/big (ABCD) word/byte order, configurable per side.
- **Site MV/LV scaling:** this installation's ND45 measures at the medium-voltage side (~9kV phase / 15588V line) via the site's step-down transformer, not at the 230V/400V point where a physical DTSU666/Sigen Sensor is normally mounted. `nd45_source` therefore carries a non-1.0 `scale` on `u_l1/l2/l3/l12/l23/l31` (÷37.5, the nameplate MV/LV ratio) and `i_l1/l2/l3` (×37.5) so canonical voltage/current already represent the equivalent LV-side reading before any DTSU encoding. Power/energy points are left unscaled since `P = U·I` is conserved across an (assumed-ideal) transformer — do not also scale `p_*`/`q_*`/`s_*`. If the transformer ratio is confirmed on-site to differ from 37.5, update only these six `scale` values.

## Things that only make sense across files

- **Two clocks in `monitor`/`poller`/`metrics` are intentional:** data-age uses the asyncio loop clock (`loop.time()`, what the poller stamps into the store); output-request timing uses `time.monotonic()` (what `RecordingSlaveContext` stamps). Keep each metric on its own clock -- `metrics.render()` takes both clocks as arguments and never calls one itself.
- **Tests never open a real serial port or TCP socket.** The poller is tested with a fake duck-typed client; the DTSU output server (either transport) is tested at the datastore level (`getValues`/`setValues`) and the supervisor with an injected `server_factory` + fake clock. Real RS-485, real TCP sockets, and live Sigenergy behavior are out of scope for the suite. The metrics endpoint follows this too: its HTTP layer is covered through the pure `parse_request`/`route`/`_response` functions, never a real listener.
- **On-hardware verification is deliberately deferred to bring-up** (see `README.md` on-site checklist): sign convention (import/export), phase order L1/L2/L3→A/B/C, scaling, word/byte order, RS-485 direction control (RTU transport only), and whether Sigenergy actually enters safe-mode on meter timeout. These cannot be confirmed by the test suite; don't treat them as code bugs.

## Reference

Register mapping references: `docs/register-map.md` (ND45 bridge) and `docs/smartlogger-dtsu-map.md` (SmartLogger bridge — tables generated from `config/registers.json` by `scripts/gen_smartlogger_map_doc.py`; re-run it after editing the `huawei_*` sections, `tests/test_docs.py` fails otherwise). Design and plan docs live in `docs/superpowers/specs/` and `docs/superpowers/plans/`. The device manuals are PDFs in the repo root (not indexed by glob — use `Get-ChildItem -Recurse -Force`); extract text with `pdfplumber` (poppler is unavailable in this environment).
