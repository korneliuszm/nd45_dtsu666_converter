# Configurable DTSU666 Identity Registers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the DTSU666 identity/config register block (0x0000-0x002E), currently hardcoded in `_STATIC_INT16_REGISTERS`, editable by hand in `config/config.json` via a new `dtsu.identity` section, so the bridge can be made to match a specific real meter's identity values (firmware/programming code, CT/PT ratio, network mode, etc.).

**Architecture:** Add a `DtsuIdentityConf` Pydantic model (one field per register, defaults = current hardcoded values) nested under `DtsuConf.identity`. `write_static_registers` in `dtsu_server.py` builds its address->value map from `dtsu_cfg.identity` instead of the module-level constant; the field-name->address mapping stays hardcoded (it's fixed by the DTSU666 protocol). `bAud`/`Addr` are unchanged (already config-driven from `rtu.baudrate`/`slave_id`).

**Tech Stack:** Python 3.10+, Pydantic v2, pytest. Run tests with `.venv/bin/python -m pytest -q`; lint with `.venv/bin/python -m ruff check .`.

## Global Constraints

- Existing `config/config.json` without a `dtsu.identity` section must produce byte-identical register values to current behavior (defaults match `_STATIC_INT16_REGISTERS`).
- Register **addresses** (0x0000, 0x0001, ... 0x002C) stay hardcoded in `dtsu_server.py` — only the **values** move to config.
- `bAud` (0x002D) and `Addr` (0x002E) are out of scope — already config-driven, unchanged.
- Lint: `ruff check .` (line-length 100) must stay clean.
- No range validation on register values beyond Pydantic's `int` type check (matches existing light-validation style).

---

### Task 1: Add `DtsuIdentityConf` model and wire it into `DtsuConf`

**Files:**
- Modify: `src/nd45_dtsu666/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `DtsuIdentityConf` (Pydantic `BaseModel`) with fields `rev: int = 100`, `ucode: int = 0`, `clr_e: int = 0`, `net: int = 0`, `ir_at: int = 10`, `ur_at: int = 10`, `disp: int = 0`, `b_lcd: int = 0`, `endian: int = 0`, `protocol: int = 0`. `DtsuConf.identity: DtsuIdentityConf` (default `DtsuIdentityConf()`).
- Consumes: nothing new (pure addition to existing `config.py`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
from nd45_dtsu666.config import DtsuIdentityConf


def test_dtsu_identity_conf_defaults_match_legacy_static_registers():
    identity = DtsuIdentityConf()
    assert identity.rev == 100
    assert identity.ucode == 0
    assert identity.clr_e == 0
    assert identity.net == 0
    assert identity.ir_at == 10
    assert identity.ur_at == 10
    assert identity.disp == 0
    assert identity.b_lcd == 0
    assert identity.endian == 0
    assert identity.protocol == 0


def test_dtsu_conf_identity_defaults_when_omitted():
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/ttyAMA2"))
    assert cfg.identity.ir_at == 10
    assert cfg.identity.ur_at == 10


def test_dtsu_conf_identity_accepts_overrides():
    cfg = DtsuConf(
        transport="rtu",
        slave_id=1,
        rtu=DtsuRtuConf(port="/dev/ttyAMA2"),
        identity=DtsuIdentityConf(rev=103, ucode=701, ir_at=200, ur_at=10),
    )
    assert cfg.identity.rev == 103
    assert cfg.identity.ucode == 701
    assert cfg.identity.ir_at == 200
    assert cfg.identity.net == 0  # untouched fields keep their default
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_config.py -k identity -v`
Expected: FAIL with `ImportError: cannot import name 'DtsuIdentityConf'`

- [ ] **Step 3: Implement `DtsuIdentityConf` and wire it into `DtsuConf`**

In `src/nd45_dtsu666/config.py`, add the new model directly above `class DtsuConf(BaseModel):`:

```python
class DtsuIdentityConf(BaseModel):
    rev: int = 100
    ucode: int = 0
    clr_e: int = 0
    net: int = 0
    ir_at: int = 10
    ur_at: int = 10
    disp: int = 0
    b_lcd: int = 0
    endian: int = 0
    protocol: int = 0
```

Then add the field to `DtsuConf` (after `slave_id: int = 1`):

```python
    identity: DtsuIdentityConf = DtsuIdentityConf()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_config.py -v`
Expected: PASS (all tests in the file, including the pre-existing ones)

- [ ] **Step 5: Lint**

Run: `.venv/bin/python -m ruff check src/nd45_dtsu666/config.py tests/test_config.py`
Expected: no issues

- [ ] **Step 6: Commit**

```bash
git add src/nd45_dtsu666/config.py tests/test_config.py
git commit -m "feat: add DtsuIdentityConf for configurable identity registers"
```

---

### Task 2: Read identity register values from `DtsuConf.identity` in `write_static_registers`

**Files:**
- Modify: `src/nd45_dtsu666/dtsu_server.py:31-61`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `DtsuIdentityConf` fields from Task 1 (`rev`, `ucode`, `clr_e`, `net`, `ir_at`, `ur_at`, `disp`, `b_lcd`, `endian`, `protocol`) and existing `DtsuConf.identity`.
- Produces: `write_static_registers(slave, slave_id, dtsu_cfg)` unchanged signature; the identity block it writes now comes from `dtsu_cfg.identity` instead of the hardcoded value dict. The existing `_IDENTITY_REGISTER_ADDRS` naming below is private to this module — no other file references it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_server.py` (near `test_static_registers_seeded_from_dtsu_cfg`):

```python
def test_static_registers_use_custom_identity_from_config():
    target = load_registers("config/registers.json").dtsu_target
    cfg = DtsuConf(
        transport="rtu",
        slave_id=1,
        rtu=DtsuRtuConf(port="/dev/null", baudrate=9600),
        identity=DtsuIdentityConf(rev=103, ucode=701, ir_at=200, ur_at=10),
    )
    context = build_context(target, slave_id=1, dtsu_cfg=cfg)
    slave = context[1]
    assert slave.getValues(3, 0x0000, count=1) == [103]  # REV. overridden
    assert slave.getValues(3, 0x0001, count=1) == [701]  # UCode overridden
    assert slave.getValues(3, 0x0006, count=1) == [200]  # IrAt overridden
    assert slave.getValues(3, 0x0007, count=1) == [10]  # UrAt unchanged (default)
    assert slave.getValues(3, 0x0003, count=1) == [0]  # net untouched -> still default
```

Also add `DtsuIdentityConf` to the existing import line at the top of `tests/test_server.py`:

```python
from nd45_dtsu666.config import DtsuConf, DtsuIdentityConf, DtsuRtuConf, DtsuTcpConf, load_registers
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_server.py -k custom_identity -v`
Expected: FAIL with `ImportError: cannot import name 'DtsuIdentityConf'` (until Task 1 is merged; if run after Task 1, it instead FAILs on the assertion `== [103]` since the code still writes the hardcoded `100`)

- [ ] **Step 3: Replace the hardcoded value dict with a field->address map read from config**

In `src/nd45_dtsu666/dtsu_server.py`, replace the `_STATIC_INT16_REGISTERS` dict (lines 31-42) with a field-name -> address mapping (addresses are protocol-fixed, values now come from config):

```python
# DTSU666 identity/config registers with no ND45 measurement equivalent (signed
# int16, 1 word each -- NOT the float32 2-word format used for measurement data).
# Addresses are fixed by the DTSU666 protocol; values come from DtsuConf.identity
# (config/config.json `dtsu.identity`) so a specific real meter's identity/CT/PT
# ratios can be entered by hand without touching code.
_IDENTITY_REGISTER_ADDRS: dict[str, int] = {
    "rev": 0x0000,  # REV.     - firmware version (arbitrary; not validated by masters)
    "ucode": 0x0001,  # UCode    - programming code
    "clr_e": 0x0002,  # CLr.E    - energy clear command
    "net": 0x0003,  # net      - network mode: 0 = 3P4W, 1 = 3P3W
    "ir_at": 0x0006,  # IrAt     - current transformer ratio, x0.1 -> 10 = ratio 1.0
    "ur_at": 0x0007,  # UrAt     - voltage transformer ratio, x0.1 -> 10 = ratio 1.0
    "disp": 0x000A,  # Disp     - display rotation time
    "b_lcd": 0x000B,  # B.LCD    - backlight time
    "endian": 0x000C,  # Endian   - reserved
    "protocol": 0x002C,  # Protocol - protocol/parity selection
}
```

Then update `write_static_registers` (was lines 48-61) to read values off `dtsu_cfg.identity`:

```python
def write_static_registers(slave: ModbusSlaveContext, slave_id: int, dtsu_cfg: DtsuConf) -> None:
    """Seed the DTSU666 identity/config registers (0x0000-0x002E block) once."""
    for field, addr in _IDENTITY_REGISTER_ADDRS.items():
        value = getattr(dtsu_cfg.identity, field)
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_server.py -v`
Expected: PASS (all tests in the file, including `test_static_registers_seeded_from_dtsu_cfg`, which exercises the defaults path and must still pass unchanged)

- [ ] **Step 5: Run the full test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, no regressions elsewhere

- [ ] **Step 6: Lint**

Run: `.venv/bin/python -m ruff check src/nd45_dtsu666/dtsu_server.py tests/test_server.py`
Expected: no issues

- [ ] **Step 7: Commit**

```bash
git add src/nd45_dtsu666/dtsu_server.py tests/test_server.py
git commit -m "feat: seed DTSU666 identity registers from config instead of hardcoded values"
```

---

### Task 3: Document `dtsu.identity` in README and add a real example to `config/config.json`

**Files:**
- Modify: `README.md`
- Modify: `config/config.json`

**Interfaces:**
- Consumes: `DtsuIdentityConf` field names from Task 1.
- Produces: none (docs/config only, no code interface).

- [ ] **Step 1: Update the README's identity/config registers checklist item**

In `README.md`, replace item 8 of the "On-site verification checklist" section:

```markdown
8. **Identity/config registers (0x0000-0x002E)** — served as a direct-connect 3P4W meter
   with CT/PT ratio 1:1 (`net=0`, `IrAt=UrAt=10`); see `_STATIC_INT16_REGISTERS` in
   `dtsu_server.py`. If Sigenergy rejects the meter or misapplies scaling, check these
   against the DTSU666 manual — this hasn't been confirmed against real Sigenergy behavior.
```

with:

```markdown
8. **Identity/config registers (0x0000-0x002E)** — values are set from `dtsu.identity` in
   `config/config.json` (defaults: direct-connect 3P4W meter, CT/PT ratio 1:1 — `net=0`,
   `ir_at=ur_at=10`). To match a specific real meter, edit `dtsu.identity` by hand, e.g.:
   ```json
   "identity": {"rev": 103, "ucode": 701, "ir_at": 200, "ur_at": 10}
   ```
   Fields omitted keep their default. See `DtsuIdentityConf` in `config.py` for the full
   field list and `_IDENTITY_REGISTER_ADDRS` in `dtsu_server.py` for the register mapping.
   If Sigenergy rejects the meter or misapplies scaling, check these against the DTSU666
   manual — this hasn't been confirmed against real Sigenergy behavior.
```

- [ ] **Step 2: Add an example `identity` block to `config/config.json`**

Read the current file first (`config/config.json`), then update the `"dtsu"` line to include an `"identity"` key with the values from the real meter dump used to design this feature:

```json
{
  "nd45":   {"host": "192.168.1.10", "port": 502, "unit_id": 1, "poll_interval_s": 0.3, "timeout_s": 1.0, "reconnect_delay_s": 1.0, "reconnect_delay_max_s": 30.0},
  "dtsu":   {"transport": "rtu", "slave_id": 10, "identity": {"rev": 103, "ucode": 701, "ir_at": 200, "ur_at": 10}, "rtu": {"port": "/dev/ttyAMA2", "baudrate": 9600, "parity": "N", "stopbits": 1}, "tcp": {"host": "0.0.0.0", "port": 502}},
  "safety": {"max_data_age_s": 3.0, "check_interval_s": 0.5, "min_restart_interval_s": 5.0}
}
```

(`slave_id` moved to `10` and `rtu`/`tcp` blocks kept as-is — only the `identity` key and `slave_id` are new/changed, matching the real register dump: `Addr=10`.)

- [ ] **Step 3: Run the full test suite to confirm `config/config.json` still loads cleanly**

Run: `.venv/bin/python -m pytest tests/test_config.py -v`
Expected: PASS — `test_load_config_reads_seed` still passes since it doesn't assert on `identity`, and the new `identity` block parses without error.

- [ ] **Step 4: Commit**

```bash
git add README.md config/config.json
git commit -m "docs: document dtsu.identity config and seed a real-meter example"
```

---

## Post-plan check

- [ ] Run `.venv/bin/python -m pytest -q` — full suite green.
- [ ] Run `.venv/bin/python -m ruff check .` — clean.
- [ ] Confirm `git log --oneline -5` shows the three commits above on `main`.
