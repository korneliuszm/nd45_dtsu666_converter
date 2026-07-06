# DTSU TCP Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the DTSU666 output side of the bridge support Modbus TCP as an alternative to Modbus RTU, selected via `config/config.json`, with the DTSU666 register format completely unchanged.

**Architecture:** `config.py` gains a `transport` switch on `DtsuConf` with two optional nested configs (`DtsuRtuConf`, `DtsuTcpConf`); `dtsu_server.py` gains a `make_tcp_server`/`make_server` factory dispatch that `supervise_server` uses instead of always building a serial server. `ModbusTcpServer` and `ModbusSerialServer` share the same `serve_forever()`/`shutdown()` interface in pymodbus 3.6.9 (confirmed from source), so the fail-safe supervisor loop itself needs zero changes.

**Tech Stack:** Python 3.10+, pymodbus 3.6.x (`ModbusTcpServer`, `ModbusSerialServer`, `ModbusSocketFramer`, `ModbusRtuFramer`), pydantic 2.x (`model_validator`), pytest/pytest-asyncio.

## Global Constraints

- pymodbus is pinned to `>=3.6,<3.7` — do not use APIs outside what's confirmed in this plan.
- Do not use pymodbus `BinaryPayloadBuilder`/`Decoder` (removed in newer pymodbus) — not touched by this plan, but don't introduce it while editing nearby code.
- `config/registers.json` (the DTSU666 register map) must NOT change — output data format stays identical.
- One active transport at a time, chosen by `dtsu.transport` in `config/config.json` (`"rtu"` or `"tcp"`) — not simultaneous RTU+TCP serving.
- RTU support is kept, not removed — this is an additive switch, not a migration.
- Line length 100 (`ruff` config in `pyproject.toml`); run `python -m ruff check .` before each commit that touches `.py` files.
- Test commands use the project venv: `.venv/bin/python -m pytest -q` (Linux) or `.venv\Scripts\python.exe -m pytest -q` (Windows). The examples below use `python -m pytest`; substitute the venv interpreter per `CLAUDE.md`.

---

### Task 1: Config schema — selectable `transport` with nested RTU/TCP blocks

**Files:**
- Modify: `src/nd45_dtsu666/config.py:1-7` (imports), `src/nd45_dtsu666/config.py:56-62` (`DtsuConf`)
- Modify: `config/config.json:3` (`dtsu` section)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `DtsuRtuConf(BaseModel)` — fields `port: str`, `baudrate: int = 9600`, `parity: str = "N"`, `stopbits: int = 1`.
- Produces: `DtsuTcpConf(BaseModel)` — fields `host: str = "0.0.0.0"`, `port: int = 502`.
- Produces: `DtsuConf(BaseModel)` — fields `transport: Literal["rtu", "tcp"] = "rtu"`, `slave_id: int = 1`, `rtu: DtsuRtuConf | None = None`, `tcp: DtsuTcpConf | None = None`; raises `pydantic.ValidationError` if the block matching `transport` is `None`.
- Consumes: nothing new — `BaseModel`, `Field` already imported in `config.py`.

- [ ] **Step 1: Write failing tests in `tests/test_config.py`**

Add these imports and test functions (append to the existing file, keep the existing tests as-is):

```python
import pytest
from pydantic import ValidationError

from nd45_dtsu666.config import DtsuConf, DtsuRtuConf, DtsuTcpConf, load_config, load_registers


def test_dtsu_conf_rtu_requires_rtu_block():
    with pytest.raises(ValidationError):
        DtsuConf(transport="rtu", slave_id=1)


def test_dtsu_conf_tcp_requires_tcp_block():
    with pytest.raises(ValidationError):
        DtsuConf(transport="tcp", slave_id=1)


def test_dtsu_conf_rtu_builds_with_rtu_block():
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/ttyAMA2"))
    assert cfg.rtu.baudrate == 9600
    assert cfg.tcp is None


def test_dtsu_conf_tcp_builds_with_tcp_block():
    cfg = DtsuConf(transport="tcp", slave_id=1, tcp=DtsuTcpConf())
    assert cfg.tcp.host == "0.0.0.0"
    assert cfg.tcp.port == 502
```

Also update the existing `test_load_config_reads_seed` (it will read the new nested shape once `config/config.json` is updated in Step 3):

```python
def test_load_config_reads_seed():
    cfg = load_config("config/config.json")
    assert cfg.nd45.port == 502
    assert cfg.dtsu.slave_id == 1
    assert cfg.dtsu.transport == "rtu"
    assert cfg.dtsu.rtu.port == "/dev/ttyAMA2"
    assert cfg.dtsu.tcp.port == 502
    assert cfg.safety.max_data_age_s == 3.0
```

There's a duplicate top-level import of `load_config`/`load_registers` already at the top of the file (`from nd45_dtsu666.config import load_config, load_registers`) — replace that line with the new combined import shown above instead of adding a second import line.

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `python -m pytest tests/test_config.py -v`
Expected: `ImportError: cannot import name 'DtsuRtuConf'` (or similar) — the new classes don't exist yet, and `test_load_config_reads_seed` fails on the new assertions.

- [ ] **Step 3: Implement the config schema**

In `src/nd45_dtsu666/config.py`, change the import line (currently line 7):

```python
from pydantic import BaseModel, Field
```

to:

```python
from typing import Literal

from pydantic import BaseModel, Field, model_validator
```

Replace the existing `DtsuConf` class (currently lines 56-62):

```python
class DtsuConf(BaseModel):
    port: str
    baudrate: int = 9600
    parity: str = "N"
    stopbits: int = 1
    slave_id: int = 1
```

with:

```python
class DtsuRtuConf(BaseModel):
    port: str
    baudrate: int = 9600
    parity: str = "N"
    stopbits: int = 1


class DtsuTcpConf(BaseModel):
    host: str = "0.0.0.0"
    port: int = 502


class DtsuConf(BaseModel):
    transport: Literal["rtu", "tcp"] = "rtu"
    slave_id: int = 1
    rtu: DtsuRtuConf | None = None
    tcp: DtsuTcpConf | None = None

    @model_validator(mode="after")
    def _check_transport_config(self) -> "DtsuConf":
        if self.transport == "rtu" and self.rtu is None:
            raise ValueError("dtsu.rtu config required when transport='rtu'")
        if self.transport == "tcp" and self.tcp is None:
            raise ValueError("dtsu.tcp config required when transport='tcp'")
        return self
```

- [ ] **Step 4: Update `config/config.json`**

Replace the `dtsu` line (currently line 3):

```json
  "dtsu":   {"port": "/dev/ttyAMA2", "baudrate": 9600, "parity": "N", "stopbits": 1, "slave_id": 1},
```

with:

```json
  "dtsu":   {"transport": "rtu", "slave_id": 1, "rtu": {"port": "/dev/ttyAMA2", "baudrate": 9600, "parity": "N", "stopbits": 1}, "tcp": {"host": "0.0.0.0", "port": 502}},
```

- [ ] **Step 5: Run the tests to confirm they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: all tests PASS, including `test_load_config_reads_seed` and the four new `test_dtsu_conf_*` tests.

- [ ] **Step 6: Run the full suite to check for fallout**

Run: `python -m pytest -q`
Expected: `tests/test_server.py` and `tests/test_app.py` now FAIL (they construct `DtsuConf(port=..., ...)` with the old flat shape) — this is expected and fixed in Task 2. Confirm no *other* files fail.

- [ ] **Step 7: Commit**

```bash
git add src/nd45_dtsu666/config.py config/config.json tests/test_config.py
git commit -m "feat: add selectable dtsu.transport (rtu/tcp) config schema"
```

---

### Task 2: TCP server factory, transport dispatch, and static-register baud handling

**Files:**
- Modify: `src/nd45_dtsu666/dtsu_server.py`
- Test/Modify: `tests/test_server.py`

**Interfaces:**
- Consumes: `DtsuConf`, `DtsuRtuConf`, `DtsuTcpConf` from Task 1 (`src/nd45_dtsu666/config.py`).
- Produces: `make_tcp_server(cfg: DtsuConf, context) -> ModbusTcpServer`.
- Produces: `make_server(cfg: DtsuConf, context) -> ModbusSerialServer | ModbusTcpServer` — dispatches on `cfg.transport`; this becomes `supervise_server`'s default factory.
- Modifies (signature unchanged, behavior changed): `make_serial_server(cfg: DtsuConf, context) -> ModbusSerialServer` now reads `cfg.rtu.*` instead of `cfg.*`.
- Modifies: `write_static_registers(slave, slave_id, dtsu_cfg)` — baud register now derived from `dtsu_cfg.transport`.

- [ ] **Step 1: Fix existing tests broken by the Task 1 config shape change**

In `tests/test_server.py`, update the import line (currently):

```python
from nd45_dtsu666.config import DtsuConf, load_registers
```

to:

```python
from nd45_dtsu666.config import DtsuConf, DtsuRtuConf, DtsuTcpConf, load_registers
```

Replace each old-shape `DtsuConf(...)` construction with the new nested shape:

`test_static_registers_seeded_from_dtsu_cfg` — replace:
```python
    cfg = DtsuConf(port="/dev/null", baudrate=9600, slave_id=7)
```
with:
```python
    cfg = DtsuConf(transport="rtu", slave_id=7, rtu=DtsuRtuConf(port="/dev/null", baudrate=9600))
```

`test_static_registers_unknown_baudrate_defaults_to_9600_code` — replace:
```python
    cfg = DtsuConf(port="/dev/null", baudrate=115200, slave_id=1)
```
with:
```python
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/null", baudrate=115200))
```

`test_supervisor_starts_when_fresh_and_stops_when_stale` — replace:
```python
    cfg = DtsuConf(port="/dev/null", slave_id=1)
```
with:
```python
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/null"))
```

`test_supervisor_retries_when_serve_fails` — replace:
```python
    cfg = DtsuConf(port="/dev/null", slave_id=1)
```
with:
```python
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/null"))
```

`test_supervisor_throttles_restart_within_interval` — replace:
```python
    cfg = DtsuConf(port="/dev/null", slave_id=1)
```
with:
```python
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/null"))
```

- [ ] **Step 2: Run `test_server.py` to confirm the fix**

Run: `python -m pytest tests/test_server.py -v`
Expected: all existing tests PASS again (no new functionality yet, just unblocked by the config-shape fix).

- [ ] **Step 3: Write failing tests for the new TCP/dispatch functionality**

Append to `tests/test_server.py`:

```python
from pymodbus.server import ModbusSerialServer, ModbusTcpServer

from nd45_dtsu666.dtsu_server import make_serial_server, make_server, make_tcp_server


def test_make_tcp_server_uses_tcp_config():
    cfg = DtsuConf(transport="tcp", slave_id=1, tcp=DtsuTcpConf(host="127.0.0.1", port=1502))
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    server = make_tcp_server(cfg, context)
    assert isinstance(server, ModbusTcpServer)
    assert server.comm_params.source_address == ("127.0.0.1", 1502)


def test_make_serial_server_uses_rtu_config():
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/ttyUSB9", baudrate=19200))
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    server = make_serial_server(cfg, context)
    assert isinstance(server, ModbusSerialServer)
    assert server.comm_params.baudrate == 19200


def test_make_server_dispatches_by_transport():
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    tcp_cfg = DtsuConf(transport="tcp", slave_id=1, tcp=DtsuTcpConf(port=1502))
    rtu_cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/null"))
    assert isinstance(make_server(tcp_cfg, context), ModbusTcpServer)
    assert isinstance(make_server(rtu_cfg, context), ModbusSerialServer)


def test_static_registers_tcp_transport_uses_fixed_baud_code():
    target = load_registers("config/registers.json").dtsu_target
    cfg = DtsuConf(transport="tcp", slave_id=1, tcp=DtsuTcpConf())
    context = build_context(target, slave_id=1, dtsu_cfg=cfg)
    assert context[1].getValues(3, 0x002D, count=1) == [3]  # bAud = fixed 9600 code
```

Note: `build_context` is already imported at the top of `tests/test_server.py`.

- [ ] **Step 4: Run the tests to confirm they fail**

Run: `python -m pytest tests/test_server.py -v`
Expected: `ImportError: cannot import name 'make_tcp_server'` (or `make_server`) — not implemented yet.

- [ ] **Step 5: Implement the TCP server factory and transport dispatch**

In `src/nd45_dtsu666/dtsu_server.py`, update the module docstring (line 1):

```python
"""DTSU666 RTU server: datastore build/update + fail-safe supervisor."""
```

to:

```python
"""DTSU666 output server (RTU or TCP): datastore build/update + fail-safe supervisor."""
```

Update the pymodbus imports (currently lines 16-17):

```python
from pymodbus.framer.rtu_framer import ModbusRtuFramer
from pymodbus.server import ModbusSerialServer
```

to:

```python
from pymodbus.framer.rtu_framer import ModbusRtuFramer
from pymodbus.framer.socket_framer import ModbusSocketFramer
from pymodbus.server import ModbusSerialServer, ModbusTcpServer
```

Update `write_static_registers` (currently lines 47-56):

```python
def write_static_registers(slave: ModbusSlaveContext, slave_id: int, dtsu_cfg: DtsuConf) -> None:
    """Seed the DTSU666 identity/config registers (0x0000-0x002E block) once."""
    for addr, value in _STATIC_INT16_REGISTERS.items():
        slave.setValues(_HOLDING_FC, addr, [value])
    baud_code = _BAUD_CODES.get(dtsu_cfg.baudrate)
    if baud_code is None:
        log.warning("No bAud code for baudrate=%d; defaulting to 9600 code", dtsu_cfg.baudrate)
        baud_code = _BAUD_CODES[9600]
    slave.setValues(_HOLDING_FC, 0x002D, [baud_code])  # bAud
    slave.setValues(_HOLDING_FC, 0x002E, [slave_id])  # Addr
```

to:

```python
def write_static_registers(slave: ModbusSlaveContext, slave_id: int, dtsu_cfg: DtsuConf) -> None:
    """Seed the DTSU666 identity/config registers (0x0000-0x002E block) once."""
    for addr, value in _STATIC_INT16_REGISTERS.items():
        slave.setValues(_HOLDING_FC, addr, [value])
    # bAud reflects a physical baudrate only in RTU mode; TCP has none, so it
    # reports a fixed 9600 code -- the register stays in the map either way
    # since the DTSU666 format must not change.
    baudrate = dtsu_cfg.rtu.baudrate if dtsu_cfg.transport == "rtu" else 9600
    baud_code = _BAUD_CODES.get(baudrate)
    if baud_code is None:
        log.warning("No bAud code for baudrate=%d; defaulting to 9600 code", baudrate)
        baud_code = _BAUD_CODES[9600]
    slave.setValues(_HOLDING_FC, 0x002D, [baud_code])  # bAud
    slave.setValues(_HOLDING_FC, 0x002E, [slave_id])  # Addr
```

Update `make_serial_server` and add `make_tcp_server`/`make_server` (currently lines 135-144):

```python
def make_serial_server(cfg, context) -> ModbusSerialServer:
    return ModbusSerialServer(
        context=context,
        framer=ModbusRtuFramer,
        port=cfg.port,
        baudrate=cfg.baudrate,
        parity=cfg.parity,
        stopbits=cfg.stopbits,
        bytesize=8,
    )
```

to:

```python
def make_serial_server(cfg: DtsuConf, context) -> ModbusSerialServer:
    return ModbusSerialServer(
        context=context,
        framer=ModbusRtuFramer,
        port=cfg.rtu.port,
        baudrate=cfg.rtu.baudrate,
        parity=cfg.rtu.parity,
        stopbits=cfg.rtu.stopbits,
        bytesize=8,
    )


def make_tcp_server(cfg: DtsuConf, context) -> ModbusTcpServer:
    return ModbusTcpServer(
        context=context,
        framer=ModbusSocketFramer,
        address=(cfg.tcp.host, cfg.tcp.port),
    )


def make_server(cfg: DtsuConf, context) -> ModbusSerialServer | ModbusTcpServer:
    if cfg.transport == "tcp":
        return make_tcp_server(cfg, context)
    return make_serial_server(cfg, context)
```

Update `supervise_server`'s default factory and log messages so they no longer hardcode "RTU" and reflect the actual transport in use. Currently:

```python
    """Start the RTU server while data is fresh; stop it (silence) while stale."""
    factory = server_factory or (lambda: make_serial_server(cfg, context))
```

to:

```python
    """Start the DTSU output server (RTU or TCP, per cfg.transport) while data is
    fresh; stop it (silence) while stale."""
    factory = server_factory or (lambda: make_server(cfg, context))
```

And the four log call sites inside `supervise_server`, currently:

```python
                    log.error("RTU server task failed: %r; will retry", exc)
                else:
                    log.warning("RTU server task ended unexpectedly; will retry")
```

to:

```python
                    log.error("DTSU server task failed: %r; will retry", exc)
                else:
                    log.warning("DTSU server task ended unexpectedly; will retry")
```

and currently:

```python
                log.info("RTU server started (data fresh, age=%.2fs)", age)
```

to:

```python
                log.info("DTSU server started (transport=%s, data fresh, age=%.2fs)", cfg.transport, age)
```

and currently:

```python
                log.warning("RTU server stopped (data stale, age=%.2fs) -> fail-safe", age)
```

to:

```python
                log.warning(
                    "DTSU server stopped (transport=%s, data stale, age=%.2fs) -> fail-safe",
                    cfg.transport, age,
                )
```

- [ ] **Step 6: Run `test_server.py` to confirm all tests pass**

Run: `python -m pytest tests/test_server.py -v`
Expected: all tests PASS, including the 4 new ones from Step 3.

- [ ] **Step 7: Run the full suite to check for fallout**

Run: `python -m pytest -q`
Expected: all tests PASS (including `tests/test_app.py`, which uses `build_pipeline`/`load_config` and is unaffected since it never constructs `DtsuConf` directly).

- [ ] **Step 8: Lint**

Run: `python -m ruff check .`
Expected: no errors. If line-length violations appear on the new log lines, wrap them (they're already written multi-line above for this reason).

- [ ] **Step 9: Commit**

```bash
git add src/nd45_dtsu666/dtsu_server.py tests/test_server.py
git commit -m "feat: dispatch DTSU output server between RTU and TCP by config"
```

---

### Task 3: CLI/diagnostics message and docstring cleanup

**Files:**
- Modify: `src/nd45_dtsu666/diagnostics.py:63`
- Modify: `src/nd45_dtsu666/app.py:1,102`

**Interfaces:**
- Consumes: `config.dtsu.transport` (from Task 1) — already in scope as the `config` parameter in `_run_selftest`.
- No new interfaces produced (text-only changes); nothing downstream depends on these strings.

- [ ] **Step 1: Update the selftest message in `diagnostics.py`**

Replace (currently line 63):

```python
        print("selftest: serving synthetic DTSU data; point mbpoll at the RTU port. Ctrl-C to stop.")
```

with:

```python
        print(
            f"selftest: serving synthetic DTSU data over {config.dtsu.transport} "
            "(see config/config.json); bench with mbpoll. Ctrl-C to stop."
        )
```

- [ ] **Step 2: Update the two RTU-specific docstrings in `app.py`**

Replace (currently line 1):

```python
"""Orchestration: wire poller + RTU server + fail-safe under one asyncio loop."""
```

with:

```python
"""Orchestration: wire poller + DTSU output server + fail-safe under one asyncio loop."""
```

Replace (currently line 102):

```python
    """Wire poller + RTU server + fail-safe. Pass `activity` to record RTU reads."""
```

with:

```python
    """Wire poller + DTSU output server + fail-safe. Pass `activity` to record read requests."""
```

- [ ] **Step 3: Run the diagnostics and app test files to confirm no regression**

Run: `python -m pytest tests/test_diagnostics.py tests/test_app.py -v`
Expected: all PASS (neither file asserts on these strings, so this just confirms nothing else broke).

- [ ] **Step 4: Lint**

Run: `python -m ruff check .`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add src/nd45_dtsu666/diagnostics.py src/nd45_dtsu666/app.py
git commit -m "docs: generalize RTU-specific log/CLI wording to cover TCP transport"
```

---

### Task 4: Documentation — README, CLAUDE.md, package metadata

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `pyproject.toml:8`
- Modify: `src/nd45_dtsu666/__init__.py:1`

**Interfaces:** None — documentation only, no code interfaces.

- [ ] **Step 1: Update `README.md`**

Replace the intro line (currently line 3):

```
Temporary Modbus bridge: Lumel ND45 (Modbus TCP) → DTSU666 register map (Modbus RTU)
so a Sigenergy storage system can read it as a "Power Sensor".
```

with:

```
Temporary Modbus bridge: Lumel ND45 (Modbus TCP) → DTSU666 register map, served as
either Modbus RTU or Modbus TCP (config-selectable) so a Sigenergy storage system can
read it as a "Power Sensor".
```

Replace the config paragraph (currently lines 15-16):

```
Edit `config/config.json`: set ND45 `host`, the RS-485 device `port` (`/dev/ttyAMA2` etc.),
`slave_id`, and `baudrate`.
```

with:

```
Edit `config/config.json`: set ND45 `host`, and under `dtsu` pick the output
`transport`:
- `"rtu"`: fill in `dtsu.rtu` — the RS-485 device `port` (`/dev/ttyAMA2` etc.),
  `baudrate`, `parity`, `stopbits`.
- `"tcp"`: fill in `dtsu.tcp` — `host` to bind (`0.0.0.0` for all interfaces) and `port`
  (502 is the Modbus TCP default).

`dtsu.slave_id` applies to both transports (RS-485 slave address for RTU, unit id in
the MBAP header for TCP) and must match what Sigenergy is configured to poll.
```

Replace the bench-test section (currently lines 18-25):

```
## Bench test before connecting Sigenergy
Run the RTU side with synthetic data and read it with mbpoll:
```bash
python -m nd45_dtsu666 selftest
# in another shell (RTU master), read holding registers as float from address 8192:
mbpoll -m rtu -a 1 -b 9600 -P none -t 4:float -r 8193 -c 4 /dev/ttyUSB0
```
(Note mbpoll `-r` is 1-based; register 8192 → `-r 8193`. Confirm word order matches.)
```

with:

```
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
```

Replace the `monitor` paragraph (currently lines 32-41), keeping it accurate for both transports:

```
Runs the live bridge (poller + RTU server + fail-safe) **and** shows a live dashboard:
the ND45 values per phase (with IMPORT/EXPORT power direction — the key sign-convention
check) plus the Modbus RTU read requests coming from Sigenergy (count, rate, which register
blocks it reads). Use this instead of `run` while bringing the system up.
```bash
python -m nd45_dtsu666 monitor   # Ctrl-C to quit
```
Requires the real RS-485 port (like `run`); for a bench test without Sigenergy use `selftest`.
During fail-safe (stale ND45) the RTU panel shows `FAIL-SAFE SILENT` — that is expected.
```

with:

```
Runs the live bridge (poller + DTSU output server + fail-safe) **and** shows a live
dashboard: the ND45 values per phase (with IMPORT/EXPORT power direction — the key
sign-convention check) plus the Modbus read requests coming from Sigenergy (count, rate,
which register blocks it reads), over whichever transport is configured. Use this
instead of `run` while bringing the system up.
```bash
python -m nd45_dtsu666 monitor   # Ctrl-C to quit
```
Requires the configured output transport to be reachable (real RS-485 port for `"rtu"`,
a free TCP port for `"tcp"`) — same requirement as `run`; for a bench test without
Sigenergy use `selftest`.
During fail-safe (stale ND45) the panel shows `FAIL-SAFE SILENT` — that is expected.
```

Update checklist items 5 and 7 (currently lines 57 and 60-61):

```
5. **Slave ID / baud** — match what Sigenergy polls.
```

with:

```
5. **Slave ID / transport params** — match what Sigenergy polls: slave/unit id always;
   baud/parity/stopbits for RTU, or host/port for TCP.
```

and:

```
7. **RS-485 direction** — verify the reComputer transceiver auto-toggles direction, or
   configure pyserial RS-485 mode if the master sees no/garbled replies.
```

with:

```
7. **RS-485 direction** (RTU transport only) — verify the reComputer transceiver
   auto-toggles direction, or configure pyserial RS-485 mode if the master sees
   no/garbled replies. Not applicable when `dtsu.transport` is `"tcp"`.
```

- [ ] **Step 2: Update `CLAUDE.md`**

Replace the "What this is" paragraph (currently line 7):

```
A **temporary Modbus protocol bridge**: it polls a Lumel **ND45** power analyzer over **Modbus TCP** (as client) and re-serves that data as a CHINT **DTSU666** power meter over **Modbus RTU / RS-485** (as server), so a **Sigenergy** battery system can read it as its "Power Sensor". It is a bridging solution meant to run for a few months until a physical DTSU666 meter arrives — favor short, safe, working changes over long-term architecture.
```

with:

```
A **temporary Modbus protocol bridge**: it polls a Lumel **ND45** power analyzer over **Modbus TCP** (as client) and re-serves that data as a CHINT **DTSU666** power meter (as server), so a **Sigenergy** battery system can read it as its "Power Sensor". The output transport is config-selectable: **Modbus RTU / RS-485** or **Modbus TCP** (`dtsu.transport` in `config/config.json`; see `docs/superpowers/specs/2026-07-06-dtsu-tcp-transport-design.md`). It is a bridging solution meant to run for a few months until a physical DTSU666 meter arrives — favor short, safe, working changes over long-term architecture.
```

Replace the architecture diagram line (currently line 40):

```
Sigenergy (RTU master) --FC03--> RTU server (serves instantly from datastore, never waits on TCP)
```

with:

```
Sigenergy (RTU or TCP master) --FC03--> DTSU output server (serves instantly from datastore, never waits on ND45 TCP poll)
```

Replace the fail-safe bullet (currently line 46):

```
- **Fail-safe** (`dtsu_server.supervise_server`): when ND45 data is older than `safety.max_data_age_s`, the RTU server is **stopped** (goes silent) so Sigenergy detects a timeout and enters its own safe mode. It restarts automatically when data returns.
```

with:

```
- **Fail-safe** (`dtsu_server.supervise_server`): when ND45 data is older than `safety.max_data_age_s`, the DTSU output server (RTU or TCP, per `dtsu.transport`) is **stopped** (goes silent) so Sigenergy detects a timeout and enters its own safe mode. It restarts automatically when data returns.
```

Replace the monitor bullet (currently line 47):

```
- `monitor.py` shows a two-panel dashboard. RTU requests from Sigenergy are captured via `RecordingSlaveContext` (a `ModbusSlaveContext` subclass logging every `getValues` into an `RtuActivity` tracker) — enabled by passing `activity=` to `build_context`.
```

with:

```
- `monitor.py` shows a two-panel dashboard. Read requests from Sigenergy (over either transport) are captured via `RecordingSlaveContext` (a `ModbusSlaveContext` subclass logging every `getValues` into an `RtuActivity` tracker) — enabled by passing `activity=` to `build_context`.
```

Replace the register-maps paragraph's config mention (currently line 51):

```
- The maps live in **`config/registers.json`** (seeded from the device PDFs) and are edited **without touching code**. ND45 addresses are **decimal**; DTSU666 addresses are **decimal converted from the manual's hex**. `config/config.json` holds runtime params (IP, serial port, slave id, intervals, `max_data_age_s`).
```

with:

```
- The maps live in **`config/registers.json`** (seeded from the device PDFs) and are edited **without touching code**. ND45 addresses are **decimal**; DTSU666 addresses are **decimal converted from the manual's hex**. `config/config.json` holds runtime params (ND45 IP, output transport + its params, slave id, intervals, `max_data_age_s`).
```

Replace the two-clocks bullet's RTU mention (currently line 60):

```
- **Two clocks in `monitor`/`poller` are intentional:** data-age uses the asyncio loop clock (`loop.time()`, what the poller stamps into the store); RTU-request timing uses `time.monotonic()` (what `RecordingSlaveContext` stamps). Keep each metric on its own clock.
```

with:

```
- **Two clocks in `monitor`/`poller` are intentional:** data-age uses the asyncio loop clock (`loop.time()`, what the poller stamps into the store); output-request timing uses `time.monotonic()` (what `RecordingSlaveContext` stamps). Keep each metric on its own clock.
```

Replace the "Tests never open a real serial port" bullet (currently line 61):

```
- **Tests never open a real serial port.** The poller is tested with a fake duck-typed client; the RTU server is tested at the datastore level (`getValues`/`setValues`) and the supervisor with an injected `server_factory` + fake clock. Real RS-485 and live Sigenergy behavior are out of scope for the suite.
```

with:

```
- **Tests never open a real serial port or TCP socket.** The poller is tested with a fake duck-typed client; the DTSU output server (either transport) is tested at the datastore level (`getValues`/`setValues`) and the supervisor with an injected `server_factory` + fake clock. Real RS-485, real TCP sockets, and live Sigenergy behavior are out of scope for the suite.
```

Replace the on-hardware verification bullet's RS-485 mention (currently line 62):

```
- **On-hardware verification is deliberately deferred to bring-up** (see `README.md` on-site checklist): sign convention (import/export), phase order L1/L2/L3→A/B/C, scaling, word/byte order, RS-485 direction control, and whether Sigenergy actually enters safe-mode on meter timeout. These cannot be confirmed by the test suite; don't treat them as code bugs.
```

with:

```
- **On-hardware verification is deliberately deferred to bring-up** (see `README.md` on-site checklist): sign convention (import/export), phase order L1/L2/L3→A/B/C, scaling, word/byte order, RS-485 direction control (RTU transport only), and whether Sigenergy actually enters safe-mode on meter timeout. These cannot be confirmed by the test suite; don't treat them as code bugs.
```

Also fix the `diag` command comment (currently `CLAUDE.md:25`):

```
python -m nd45_dtsu666 diag                        # standalone ND45 poll table (no RTU serving)
```

with:

```
python -m nd45_dtsu666 diag                        # standalone ND45 poll table (no output serving)
```

- [ ] **Step 3: Update `pyproject.toml` description and `__init__.py` docstring**

Replace (currently `pyproject.toml:8`):

```toml
description = "Modbus bridge: Lumel ND45 (TCP) -> DTSU666 (RTU) for Sigenergy"
```

with:

```toml
description = "Modbus bridge: Lumel ND45 (TCP) -> DTSU666 (RTU or TCP, config-selectable) for Sigenergy"
```

Replace (currently `src/nd45_dtsu666/__init__.py:1`):

```python
"""Modbus bridge: Lumel ND45 (TCP) -> DTSU666 (RTU) for Sigenergy."""
```

with:

```python
"""Modbus bridge: Lumel ND45 (TCP) -> DTSU666 (RTU or TCP, config-selectable) for Sigenergy."""
```

- [ ] **Step 4: Run the full test suite and lint as a final check**

Run: `python -m pytest -q`
Expected: all tests PASS.

Run: `python -m ruff check .`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md pyproject.toml src/nd45_dtsu666/__init__.py
git commit -m "docs: document config-selectable RTU/TCP output transport"
```

---

### Task 5: Manual smoke test of the TCP transport

**Files:** none (verification only — exercises the code from Tasks 1-2 end-to-end).

- [ ] **Step 1: Create a TCP-mode config copy**

```bash
python - <<'EOF'
import json
cfg = json.load(open("config/config.json"))
cfg["dtsu"]["transport"] = "tcp"
cfg["dtsu"]["tcp"] = {"host": "127.0.0.1", "port": 15020}
json.dump(cfg, open("/tmp/dtsu_tcp_smoke.json", "w"))
EOF
cp config/registers.json /tmp/dtsu_tcp_smoke_registers.json
```

- [ ] **Step 2: Run `selftest` with the TCP config in the background**

Run: `python -m nd45_dtsu666 --config /tmp/dtsu_tcp_smoke.json --registers /tmp/dtsu_tcp_smoke_registers.json selftest &`
Expected stdout: `selftest: serving synthetic DTSU data over tcp (see config/config.json); bench with mbpoll. Ctrl-C to stop.`

- [ ] **Step 3: Read a register over Modbus TCP to confirm it actually serves**

Run (after a 1-2s pause for the server to bind):
```bash
mbpoll -m tcp -a 1 -t 4:float -r 8193 -c 1 127.0.0.1 -p 15020
```
Expected: a plausible float value is printed (u_l1 synthetic value is 230.0 per `_synthetic_values` in `diagnostics.py`), confirming a real TCP Modbus client can read the DTSU666 register over TCP.

If `mbpoll` is not installed, substitute a short Python check instead:
```bash
python - <<'EOF'
from pymodbus.client import ModbusTcpClient
c = ModbusTcpClient("127.0.0.1", port=15020)
c.connect()
r = c.read_holding_registers(8192, 2, slave=1)
print(r.registers)
c.close()
EOF
```
Expected: prints a 2-element list of register words (non-`[0, 0]`, since synthetic data is being fed).

- [ ] **Step 4: Stop the background process and clean up**

```bash
kill %1
rm -f /tmp/dtsu_tcp_smoke.json /tmp/dtsu_tcp_smoke_registers.json
```

- [ ] **Step 5: Report result**

No commit for this task (verification only). If Step 3 fails, treat it as a bug in Task 2's `make_tcp_server`/`supervise_server` wiring and go back to fix it before considering the plan done.
