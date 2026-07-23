# Sigen Register Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the confirmed Sigen OEM FC04 measurement map and FC03 identity/handshake while retaining the classic DTSU666 FC03 map.

**Architecture:** Extend the register-map model with function-code-aware targets and validated static identity points. Build separate holding/input datastore blocks, seed Sigen identity into holding registers, and update both measurement targets from the same canonical SI snapshot.

**Tech Stack:** Python 3.10+, Pydantic 2, pymodbus 3.6, pytest, JSON configuration.

## Global Constraints

- Preserve the classic DTSU666 FC03 map, scales, signs, and energy points.
- Serve Sigen measurements through FC04 with float32 big/big encoding and scale `1`.
- Serve `Sigen Sensor TPX-CH\0` in a 40-byte FC03 field at `0xF100`.
- Serve handshake words `0x0000, 0x1500` at `0xF114`.
- Do not invent Sigen energy addresses.
- Keep the existing stale-data fail-safe behavior.

---

### Task 1: Function-code-aware register configuration

**Files:**
- Modify: `tests/test_config.py`
- Modify: `src/nd45_dtsu666/config.py`
- Modify: `config/registers.json`

**Interfaces:**
- Produces: `TargetSide.function_code: Literal[3, 4]`
- Produces: `RegisterMap.dtsu_sigen_ext_target: TargetSide`
- Produces: `RegisterMap.dtsu_sigen_identity: StaticIdentitySide`

- [ ] **Step 1: Write failing seed-map tests**

Add assertions that the classic target defaults to FC03, the Sigen target uses FC04,
all Sigen points use scale 1 and the expected `-2806` address offset, and the static
identity model contains the 40-byte ASCII field plus uint32 value 5376.

- [ ] **Step 2: Verify the tests fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_config.py -q`

Expected: failure because `dtsu_sigen_ext_target` and `dtsu_sigen_identity` do not exist.

- [ ] **Step 3: Implement the validated models**

Add:

```python
class TargetSide(BaseModel):
    word_order: str = "big"
    byte_order: str = "big"
    function_code: Literal[3, 4] = 3
    points: dict[str, TargetPoint]


class StaticIdentityPoint(BaseModel):
    addr: int
    type: Literal["ascii", "uint32"]
    static_value: str | int
    length: int | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> "StaticIdentityPoint":
        if self.type == "ascii":
            if not isinstance(self.static_value, str) or self.length is None:
                raise ValueError("ascii static point requires string value and register length")
            if len(self.static_value.encode("ascii")) > self.length * 2:
                raise ValueError("ascii static value does not fit configured register length")
        elif not isinstance(self.static_value, int) or self.length is not None:
            raise ValueError("uint32 static point requires integer value and no length")
        return self


class StaticIdentitySide(BaseModel):
    function_code: Literal[3] = 3
    points: dict[str, StaticIdentityPoint]
```

Extend `RegisterMap` with the two required Sigen sections. Populate
`config/registers.json` with the confirmed points from the design.

- [ ] **Step 4: Verify configuration tests pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_config.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_config.py src/nd45_dtsu666/config.py config/registers.json
git commit -m "feat: configure Sigen register maps"
```

### Task 2: Separate FC03/FC04 datastores and static identity

**Files:**
- Modify: `tests/test_server.py`
- Modify: `src/nd45_dtsu666/dtsu_server.py`

**Interfaces:**
- Produces: `write_sigen_identity(slave, identity) -> None`
- Changes: `build_context(targets, slave_id, activity=None, dtsu_cfg=None, sigen_identity=None)`
- Changes: `update_datastore(context, slave_id, canonical, targets)`

- [ ] **Step 1: Write failing datastore tests**

Add focused tests proving:

```python
assert slave.getValues(3, 0xF100, count=20) == [
    0x5369, 0x6765, 0x6E20, 0x5365, 0x6E73,
    0x6F72, 0x2054, 0x5058, 0x2D43, 0x4800,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
]
assert slave.getValues(3, 0xF114, count=2) == [0x0000, 0x1500]
```

Also update one canonical `u_l1=230.0` and assert FC03 contains encoded `2300.0`
at `0x2006` while FC04 contains encoded `230.0` at `0x1510`.

- [ ] **Step 2: Verify the tests fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_server.py -q`

Expected: failures because FC04 has no input-register block and Sigen identity is not seeded.

- [ ] **Step 3: Implement independent data spaces**

Create one `ModbusSequentialDataBlock` per used function code, pass it as `hr` or `ir`
to `ModbusSlaveContext`, and write each target via `slave.setValues(target.function_code,
pt.addr, regs)`. Encode ASCII by padding the configured byte field with zeros and
combining each big-endian byte pair into a uint16. Encode uint32 as high word followed
by low word.

Keep existing callers compatible by accepting either one target or an iterable and
normalizing internally.

- [ ] **Step 4: Verify server tests pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_server.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_server.py src/nd45_dtsu666/dtsu_server.py
git commit -m "feat: serve Sigen FC04 and identity registers"
```

### Task 3: Wire both maps and update diagnostics

**Files:**
- Modify: `tests/test_app.py`
- Modify: `tests/test_rtudebug.py`
- Modify: `src/nd45_dtsu666/app.py`
- Modify: `src/nd45_dtsu666/diagnostics.py`
- Modify: `src/nd45_dtsu666/rtudebug.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: both target maps and static identity from `RegisterMap`
- Produces: live pipeline updates for FC03 and FC04

- [ ] **Step 1: Write failing wiring and diagnostics tests**

Assert `build_pipeline` exposes both measurement maps, FC04 reads are labeled with
Sigen point names, and FC03 lookups still include classic and identity names.

- [ ] **Step 2: Verify the tests fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_rtudebug.py -q`

Expected: failures because the pipeline and name index only receive `dtsu_target`.

- [ ] **Step 3: Wire both maps**

Pass `[registers.dtsu_target, registers.dtsu_sigen_ext_target]` and
`registers.dtsu_sigen_identity` into context construction and update callbacks.
Make diagnostic indexing function-code-aware so the same numeric address in different
data spaces cannot be mislabeled. Document FC03 identity and FC04 Sigen polling in the
README commissioning section.

- [ ] **Step 4: Verify focused tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_rtudebug.py -q`

Expected: all tests pass.

- [ ] **Step 5: Verify the complete project**

Run:

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q
```

Expected: Ruff exits 0 and pytest reports zero failures.

- [ ] **Step 6: Commit**

```powershell
git add tests/test_app.py tests/test_rtudebug.py src/nd45_dtsu666/app.py src/nd45_dtsu666/diagnostics.py src/nd45_dtsu666/rtudebug.py README.md
git commit -m "feat: enable Sigen-compatible bridge mode"
```

### Task 4: Temporarily unidentified FC04 ranges

**Files:**
- Modify: `tests/test_config.py`
- Modify: `tests/test_server.py`
- Modify: `tests/test_rtudebug.py`
- Modify: `src/nd45_dtsu666/config.py`
- Modify: `src/nd45_dtsu666/dtsu_server.py`
- Modify: `src/nd45_dtsu666/rtudebug.py`
- Modify: `config/registers.json`
- Modify: `README.md`

**Interfaces:**
- Produces: `RegisterMap.dtsu_sigen_zero_ranges: StaticZeroSide`
- Produces: zero-filled FC04 responses for `0x180A` count 22 and `0x1828` count 4

- [ ] **Step 1: Write failing configuration, datastore, and diagnostics tests**

Assert that the seed map declares the two exact FC04 ranges, both live reads
return lists of zero words, and the register-name index labels both requests as
temporarily unidentified Sigen ranges.

- [ ] **Step 2: Verify the tests fail**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_server.py tests/test_rtudebug.py -q
```

Expected: failure because `dtsu_sigen_zero_ranges` does not exist and FC04
validation rejects addresses above `0x154F`.

- [ ] **Step 3: Implement configured zero ranges**

Add validated `StaticZeroRange(addr: int, count: int, name: str)` and
`StaticZeroSide(function_code: Literal[4], ranges: list[StaticZeroRange])`.
Include range ends when sizing the FC04 datastore and add the ranges to the
diagnostic index. Do not write canonical values into them.

- [ ] **Step 4: Verify focused tests pass**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_server.py tests/test_rtudebug.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Verify lint and the platform-compatible suite**

Run:

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q --ignore=tests/test_watchdog.py -k "not test_run_app_pings_watchdog_during_initial_connect_retry"
```

Expected: Ruff exits 0 and pytest reports zero failures.

- [ ] **Step 6: Commit**

```powershell
git add docs/superpowers/specs/2026-07-23-sigen-register-map-design.md docs/superpowers/plans/2026-07-23-sigen-register-map.md tests/test_config.py tests/test_server.py tests/test_rtudebug.py src/nd45_dtsu666/config.py src/nd45_dtsu666/dtsu_server.py src/nd45_dtsu666/rtudebug.py config/registers.json README.md
git commit -m "fix: serve unidentified Sigen FC04 ranges"
```
