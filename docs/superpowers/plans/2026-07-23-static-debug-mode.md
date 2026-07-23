# Static Debug Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve stable JSON-configured measurements to Sigenergy without connecting to the ND45.

**Architecture:** Add validated static values to application configuration and a dedicated static pipeline containing the canonical store, output datastore, freshness feeder, server supervisor, and request dashboard. Reuse existing FC03/FC04 maps and transport behavior while keeping the live pipeline unchanged.

**Tech Stack:** Python 3.10+, asyncio, Pydantic 2, pymodbus 3.6, pytest.

## Global Constraints

- Start only through `python -m nd45_dtsu666 static`.
- Never create or connect an ND45 client in static mode.
- Serve configured values exactly; fill omitted canonical outputs with `0.0`.
- Reject unknown keys, booleans, strings, NaN, and infinity.
- Keep `feed_interval_s` positive and below `safety.max_data_age_s`.
- Preserve all existing commands and output maps.

---

### Task 1: Validated static configuration

**Files:**
- Modify: `tests/test_config.py`
- Modify: `src/nd45_dtsu666/config.py`
- Modify: `config/config.json`

**Interfaces:**
- Produces: `StaticDebugConf(feed_interval_s: float, values: dict[str, float])`
- Produces: `AppConfig.static_debug: StaticDebugConf`

- [ ] **Step 1: Write failing tests**

Test the seed values, rejection of unknown keys and invalid numeric values, and
the cross-field constraint against `safety.max_data_age_s`.

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_config.py -q`

Expected: failures because `StaticDebugConf` and `static_debug` do not exist.

- [ ] **Step 3: Implement the model and seed JSON**

Add the exact canonical-key allowlist from the design, validate raw values
before Pydantic coercion, validate finiteness with `math.isfinite`, and check
the feed interval in `AppConfig`.

- [ ] **Step 4: Verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_config.py -q`

Expected: all tests pass.

### Task 2: Static pipeline and dashboard

**Files:**
- Create: `src/nd45_dtsu666/static_debug.py`
- Create: `tests/test_static_debug.py`
- Modify: `src/nd45_dtsu666/monitor.py`
- Modify: `tests/test_monitor.py`

**Interfaces:**
- Produces: `expand_static_values(registers, configured) -> dict[str, float]`
- Produces: `build_static_pipeline(config, registers, stop_event, activity) -> StaticPipeline`
- Produces: `run_static_debug(config, registers, stop_event) -> None`
- Changes: `render_dashboard(..., source_label: str = "ND45") -> str`

- [ ] **Step 1: Write failing tests**

Test zero fill, exact configured values, FC03/FC04 encoding, fresh-store feeder
behavior, lack of an ND45 client field, and `STATIC DEBUG` dashboard labeling.

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_static_debug.py tests/test_monitor.py -q`

Expected: import or assertion failures because static mode does not exist.

- [ ] **Step 3: Implement the static pipeline**

Build the same two target maps, identity, and zero ranges as the live pipeline.
Create only a feeder coroutine and `supervise_server`; do not import or
instantiate `AsyncModbusTcpClient`. Render the dashboard from the static store
and the shared `RtuActivity`.

- [ ] **Step 4: Verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_static_debug.py tests/test_monitor.py -q`

Expected: all tests pass.

### Task 3: CLI, documentation, and complete verification

**Files:**
- Modify: `src/nd45_dtsu666/__main__.py`
- Modify: `tests/test_main.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `run_static_debug`
- Produces: CLI subcommand `static`

- [ ] **Step 1: Write failing CLI tests**

Test that `_cmd_static` loads configuration/registers, installs shutdown
handling, calls the static runner, and exits cleanly on KeyboardInterrupt.

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_main.py -q`

Expected: failure because `_cmd_static` and the parser branch do not exist.

- [ ] **Step 3: Implement CLI dispatch and documentation**

Add the `static` parser and command function. Document JSON editing, the run
command, the synthetic-data warning, and stopping the normal service before
opening the same RTU port.

- [ ] **Step 4: Verify focused tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_static_debug.py tests/test_monitor.py tests/test_main.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Verify project**

Run:

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q --ignore=tests/test_watchdog.py -k "not test_run_app_pings_watchdog_during_initial_connect_retry"
```

Expected: Ruff exits 0 and pytest reports zero failures.

- [ ] **Step 6: Commit**

```powershell
git add config/config.json src/nd45_dtsu666/config.py src/nd45_dtsu666/static_debug.py src/nd45_dtsu666/monitor.py src/nd45_dtsu666/__main__.py tests/test_config.py tests/test_static_debug.py tests/test_monitor.py tests/test_main.py README.md docs/superpowers/plans/2026-07-23-static-debug-mode.md
git commit -m "feat: add configurable static debug mode"
```
