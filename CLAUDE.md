# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **temporary Modbus protocol bridge**: it polls a Lumel **ND45** power analyzer over **Modbus TCP** (as client) and re-serves that data as a CHINT **DTSU666** power meter (as server), so a **Sigenergy** battery system can read it as its "Power Sensor". The output transport is config-selectable: **Modbus RTU / RS-485** or **Modbus TCP** (`dtsu.transport` in `config/config.json`; see `docs/superpowers/specs/2026-07-06-dtsu-tcp-transport-design.md`). It is a bridging solution meant to run for a few months until a physical DTSU666 meter arrives — favor short, safe, working changes over long-term architecture.

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
python -m nd45_dtsu666 run                         # run the bridge
python -m nd45_dtsu666 monitor                     # bridge + live commissioning dashboard
python -m nd45_dtsu666 rtudebug                     # bridge + log every register block Sigenergy reads (debug)
python -m nd45_dtsu666 diag                        # standalone ND45 poll table (no output serving)
python -m nd45_dtsu666 selftest                    # serve synthetic DTSU data for mbpoll bench test
curl -s localhost:9090/metrics                     # Prometheus scrape (served by run/monitor/rtudebug/static)
```

CI (`.github/workflows/ci.yml`) runs `ruff check` + `pytest` on Python 3.10/3.11/3.12. There is no separate build step — it's a pure Python package.

## Architecture

Single-process **asyncio**, one event loop, no locks. The core data flow:

```
ND45 (TCP slave) --FC03--> nd45_poller --decode--> canonical SI store  --+--> dtsu datastore
                                                          |               (encode -> DTSU registers)
                                              dtsu_server supervisor (freshness gate)
                                                          |
Sigenergy (RTU or TCP master) --FC03--> DTSU output server (serves instantly from datastore, never waits on ND45 TCP poll)

optional, off by default:
Huawei SmartLogger (TCP) --FC03--> huawei_poller --decode+derive--> secondary store (pv_*/mtr_*)
                                    merged into the datastore, but NEVER gates the output
```

- `app.build_pipeline()` wires everything and is shared by `run` (`run_app`), `monitor` (`monitor.run_monitor`), and `rtudebug`. It returns a `Pipeline` of store, context, client, the poller+supervisor coroutines, and a `MetricsSource`. When editing the wiring, change `build_pipeline`, not the callers.
- `codec.py` is a **`struct`-based** float32 ↔ register codec, plus a sized-integer half (`u16/i16/u32/i32/u64/i64`) used only by the Huawei source. Do **not** use pymodbus `BinaryPayloadBuilder`/`Decoder` (removed in newer pymodbus, and the version is pinned to `>=3.6,<3.7`). Huawei's documented "Gain" is a divisor but needs no new concept — it folds into the map's existing `scale` multiplier.
- `canonical.py` (`CanonicalStore`, `HealthGate`) holds the latest SI values + a timestamp and is the single source of truth. `dtsu_server.update_datastore` mirrors those values into the pymodbus datastore. It also holds `compute_derived` (canonical-model logic every source must run; re-exported from `nd45_poller` for existing importers) and `apply_derive`.
- **Optional second source, Huawei SmartLogger** (`huawei_poller.py`, `huawei.enabled` in `config.json`, off by default). Reports PV production alongside the grid-tie measurement. Three things about it are load-bearing and easy to break: (1) `canonical.MergedStore` delegates `age()`/`is_fresh()` to the **primary (ND45) store only**, so a slow or absent SmartLogger cannot silence the DTSU output — its own `huawei.max_data_age_s` (60s) is telemetry-only and must not be confused with `safety.max_data_age_s` (3s); (2) the SmartLogger callback deliberately **does not** touch `Heartbeat`, or a live secondary poller would mask a hung ND45 poller from systemd; (3) the Huawei poller must **not** call `compute_derived`, which reads unprefixed energy keys a `pv_*`/`mtr_*` map does not have and would zero real ND45 energy. Point names are prefixed precisely so `build_on_update`'s `beneath=`/`above=` merge (primary always wins) keeps the two sources from displacing each other. Coverage is partial by necessity — no SmartLogger register set covers the full canonical model and grid frequency appears nowhere in its manual — so the maps carry declarative `derive` rules. See `docs/superpowers/specs/2026-07-30-huawei-smartlogger-source-design.md`.
- **Fail-safe** (`dtsu_server.supervise_server`): when ND45 data is older than `safety.max_data_age_s`, the DTSU output server (RTU or TCP, per `dtsu.transport`) is **stopped** (goes silent) so Sigenergy detects a timeout and enters its own safe mode. It restarts automatically when data returns.
- **Watchdog** (`watchdog.py`): the systemd unit's `WatchdogSec=` (read from the `WATCHDOG_USEC` env var, no duplication in `config.json`) drives a heartbeat tied to real ND45-poller progress (`Heartbeat`, touched by `connect_with_retry` and by `build_pipeline`'s `on_update`/`on_error`) — a genuine poller hang stops the pings and lets systemd restart the service; a normal ND45 outage does not, since the poller is still cycling through its error path. See `docs/superpowers/specs/2026-07-06-systemd-watchdog-design.md`.
- **Metrics** (`metrics.py`): a read-only Prometheus endpoint on `asyncio.start_server` (no `prometheus_client` -- its `start_http_server` uses a thread, and this process is one loop with no locks). It reads existing state at scrape time (`CanonicalStore`, `RtuActivity`, `Heartbeat`) plus two new holders, `PollStats` (fed by `build_pipeline`'s `on_update`/`on_error`; the poller is untouched) and `ServerStatus` (set by `supervise_server`'s optional `status=`, since server state is otherwise loop-local and cannot be re-derived from the freshness gate). Two things are easy to get wrong: the task is created **before** `connect_with_retry` in every runner -- `pipe.coros` only start after the ND45 connects, and an endpoint that is down during an ND45 outage is useless -- and `build_pipeline` now creates an `RtuActivity` itself when the endpoint is enabled, so `run` gets read stats too. See `docs/superpowers/specs/2026-07-27-prometheus-metrics-design.md`.
- `monitor.py` shows a two-panel dashboard. Read requests from Sigenergy (over either transport) are captured via `RecordingSlaveContext` (a `ModbusSlaveContext` subclass logging every `getValues` into an `RtuActivity` tracker) — enabled by passing `activity=` to `build_context`.

## Register maps and translation (the crux)

- The maps live in **`config/registers.json`** (seeded from the device PDFs) and are edited **without touching code**. ND45 addresses are **decimal**; DTSU666 addresses are **decimal converted from the manual's hex**. `config/config.json` holds runtime params (ND45 IP, output transport + its params, slave id, intervals, `max_data_age_s`).
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

Design and plan docs live in `docs/superpowers/specs/` and `docs/superpowers/plans/`. The device manuals are PDFs in the repo root (not indexed by glob — use `Get-ChildItem -Recurse -Force`); extract text with `pdfplumber` (poppler is unavailable in this environment).
