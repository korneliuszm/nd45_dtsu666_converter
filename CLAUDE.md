# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **temporary Modbus protocol bridge**: it polls a Lumel **ND45** power analyzer over **Modbus TCP** (as client) and re-serves that data as a CHINT **DTSU666** power meter over **Modbus RTU / RS-485** (as server), so a **Sigenergy** battery system can read it as its "Power Sensor". It is a bridging solution meant to run for a few months until a physical DTSU666 meter arrives — favor short, safe, working changes over long-term architecture.

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
python -m nd45_dtsu666 diag                        # standalone ND45 poll table (no RTU serving)
python -m nd45_dtsu666 selftest                    # serve synthetic DTSU data for mbpoll bench test
```

CI (`.github/workflows/ci.yml`) runs `ruff check` + `pytest` on Python 3.10/3.11/3.12. There is no separate build step — it's a pure Python package.

## Architecture

Single-process **asyncio**, one event loop, no locks. The core data flow:

```
ND45 (TCP slave) --FC03--> nd45_poller --decode--> canonical SI store  --+--> dtsu datastore
                                                          |               (encode -> DTSU registers)
                                              dtsu_server supervisor (freshness gate)
                                                          |
Sigenergy (RTU master) --FC03--> RTU server (serves instantly from datastore, never waits on TCP)
```

- `app.build_pipeline()` wires everything and is shared by both `run` (`run_app`) and `monitor` (`monitor.run_monitor`). It returns a `Pipeline` of store, context, client, and the poller+supervisor coroutines. When editing the wiring, change `build_pipeline`, not the two callers.
- `codec.py` is a **`struct`-based** float32 ↔ register codec. Do **not** use pymodbus `BinaryPayloadBuilder`/`Decoder` (removed in newer pymodbus, and the version is pinned to `>=3.6,<3.7`).
- `canonical.py` (`CanonicalStore`, `HealthGate`) holds the latest SI values + a timestamp and is the single source of truth. `dtsu_server.update_datastore` mirrors those values into the pymodbus datastore.
- **Fail-safe** (`dtsu_server.supervise_server`): when ND45 data is older than `safety.max_data_age_s`, the RTU server is **stopped** (goes silent) so Sigenergy detects a timeout and enters its own safe mode. It restarts automatically when data returns.
- `monitor.py` shows a two-panel dashboard. RTU requests from Sigenergy are captured via `RecordingSlaveContext` (a `ModbusSlaveContext` subclass logging every `getValues` into an `RtuActivity` tracker) — enabled by passing `activity=` to `build_context`.

## Register maps and translation (the crux)

- The maps live in **`config/registers.json`** (seeded from the device PDFs) and are edited **without touching code**. ND45 addresses are **decimal**; DTSU666 addresses are **decimal converted from the manual's hex**. `config/config.json` holds runtime params (IP, serial port, slave id, intervals, `max_data_age_s`).
- **Transform semantics** (implemented in `codec.decode_point`/`encode_point`, must stay exact):
  - ND45 → canonical: `SI = (raw_float * scale * sign) + offset`
  - canonical → DTSU register: `register_float = (SI * sign * scale) + offset`
  - DTSU scales are e.g. V×10, A×1000, W×10, PF×1000, Hz×100; energy is direct kWh. `sign ∈ {+1,-1}` flips import/export.
- Both sides default to big/big (ABCD) word/byte order, configurable per side.

## Things that only make sense across files

- **Two clocks in `monitor`/`poller` are intentional:** data-age uses the asyncio loop clock (`loop.time()`, what the poller stamps into the store); RTU-request timing uses `time.monotonic()` (what `RecordingSlaveContext` stamps). Keep each metric on its own clock.
- **Tests never open a real serial port.** The poller is tested with a fake duck-typed client; the RTU server is tested at the datastore level (`getValues`/`setValues`) and the supervisor with an injected `server_factory` + fake clock. Real RS-485 and live Sigenergy behavior are out of scope for the suite.
- **On-hardware verification is deliberately deferred to bring-up** (see `README.md` on-site checklist): sign convention (import/export), phase order L1/L2/L3→A/B/C, scaling, word/byte order, RS-485 direction control, and whether Sigenergy actually enters safe-mode on meter timeout. These cannot be confirmed by the test suite; don't treat them as code bugs.

## Reference

Design and plan docs live in `docs/superpowers/specs/` and `docs/superpowers/plans/`. The device manuals are PDFs in the repo root (not indexed by glob — use `Get-ChildItem -Recurse -Force`); extract text with `pdfplumber` (poppler is unavailable in this environment).
