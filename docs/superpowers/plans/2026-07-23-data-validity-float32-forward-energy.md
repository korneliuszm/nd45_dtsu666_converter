# Data Validity, Finite Float32, and Forward Energy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject invalid critical ND45 samples and unrepresentable DTSU values, while serving forward active energy at Sigen register `0x180A` and its FC03 aliases.

**Architecture:** Keep the existing canonical-store freshness fail-safe and configuration-driven register maps. Classify only power-factor readings as optional at polling time, enforce a finite-binary32 contract in the codec, stage all target encodings before datastore writes, and add three target mappings from the existing `imp_energy_total` canonical value.

**Tech Stack:** Python 3.11+, asyncio, pymodbus 3.6.9, Pydantic 2, pytest/pytest-asyncio, JSON register maps.

## Global Constraints

- `pf_l1`, `pf_l2`, `pf_l3`, and `pf_total` are the only invalid source values that may be represented as `0.0`.
- Any other ND45 value that is non-finite or has `abs(value) >= 1e20` rejects the complete poll.
- DTSU float output must always be finite IEEE-754 binary32; do not clamp overflow.
- Validate target values after sign, scale, offset, and CT conversion.
- FC03 `0x100A` and `0x1050` divide `imp_energy_total` by CT; FC04 `0x180A` does not.
- Do not infer or populate any other formerly zero-filled register in `0x180A` through `0x181D`.
- Work in the existing `main` checkout as previously approved; do not create a worktree.

---

### Task 1: Reject invalid critical ND45 samples

**Files:**
- Modify: `tests/test_poller.py:1-250`
- Modify: `src/nd45_dtsu666/nd45_poller.py:14-125`

**Interfaces:**
- Consumes: `OVERRANGE = 1e20`, `PollError`, `poll_once(...)`, and `run_poller(...)`.
- Produces: `OPTIONAL_INVALID_ZERO_POINTS: frozenset[str]`; `poll_once()` returns a sample only when all critical points are valid.

- [ ] **Step 1: Replace the old invalid-value masking tests with failing classification tests**

Add a raw IEEE-754 helper that remains able to construct invalid ND45 frames
after the production encoder starts rejecting special values:

```python
import struct


def _raw_float_registers(value: float) -> list[int]:
    return list(struct.unpack(">HH", struct.pack(">f", value)))
```

Replace `test_poll_once_substitutes_zero_for_nan`,
`test_poll_once_substitutes_zero_for_inf`, and the existing warning test with:

```python
async def test_poll_once_substitutes_zero_only_for_invalid_power_factor():
    src = load_registers("config/registers.json").nd45_source
    image = {64: _raw_float_registers(float("nan"))}

    values = await poll_once(FakeClient(image), src, slave=1)

    assert values["pf_l1"] == 0.0


@pytest.mark.parametrize(
    ("address", "value", "point"),
    [
        (50, float("nan"), "u_l1"),
        (52, float("inf"), "i_l1"),
        (128, 3e20, "p_total"),
        (912, 3e20, "imp_energy_total"),
    ],
)
async def test_poll_once_rejects_invalid_critical_value(address, value, point):
    src = load_registers("config/registers.json").nd45_source
    image = {address: _raw_float_registers(value)}

    with pytest.raises(PollError, match=point):
        await poll_once(FakeClient(image), src, slave=1)


async def test_poll_once_reports_all_invalid_critical_points():
    src = load_registers("config/registers.json").nd45_source
    image = {
        50: _raw_float_registers(float("nan")),
        52: _raw_float_registers(float("inf")),
    }

    with pytest.raises(PollError, match=r"u_l1.*i_l1"):
        await poll_once(FakeClient(image), src, slave=1)


async def test_poll_once_invalid_warning_logged_once_per_episode(caplog):
    src = load_registers("config/registers.json").nd45_source
    invalid = {50: _raw_float_registers(3e20)}
    seen: set[str] = set()
    with caplog.at_level("WARNING", logger="nd45_dtsu666.nd45_poller"):
        for _ in range(2):
            with pytest.raises(PollError):
                await poll_once(FakeClient(invalid), src, slave=1, overrange_seen=seen)
    warnings = [r for r in caplog.records if "u_l1" in r.getMessage()]
    assert len(warnings) == 1

    normal = _image_for({50: 230.0})
    values = await poll_once(FakeClient(normal), src, slave=1, overrange_seen=seen)
    assert values["u_l1"] == pytest.approx(230.0)
    assert "u_l1" not in seen
```

Import `PollError` from `nd45_poller`.

- [ ] **Step 2: Add a failing poll-loop test proving no update occurs**

```python
async def test_run_poller_does_not_publish_invalid_critical_sample():
    src = load_registers("config/registers.json").nd45_source
    client = FakeClient({50: _raw_float_registers(float("nan"))})
    stop = asyncio.Event()
    updates: list[tuple[dict[str, float], float]] = []
    errors: list[Exception] = []

    async def _stopper():
        await asyncio.sleep(0.04)
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(
            run_poller(
                client,
                src,
                slave=1,
                interval=0.01,
                on_update=lambda values, ts: updates.append((values, ts)),
                on_error=errors.append,
                stop_event=stop,
            ),
            _stopper(),
        ),
        timeout=1.0,
    )

    assert updates == []
    assert errors and all(isinstance(exc, PollError) for exc in errors)
```

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```powershell
python -m pytest tests/test_poller.py -q
```

Expected: the new critical-value tests fail because `poll_once()` returns
zero-filled fresh samples instead of raising `PollError`.

- [ ] **Step 4: Implement classification and complete-sample rejection**

In `src/nd45_dtsu666/nd45_poller.py`, add:

```python
OPTIONAL_INVALID_ZERO_POINTS = frozenset(
    {"pf_l1", "pf_l2", "pf_l3", "pf_total"}
)
```

Inside `poll_once()`, initialize `invalid_critical: list[str] = []` before the
point loop. Replace the current invalid-value branch with:

```python
        invalid = not math.isfinite(si) or abs(si) >= OVERRANGE
        if invalid:
            optional = key in OPTIONAL_INVALID_ZERO_POINTS
            if overrange_seen is None or key not in overrange_seen:
                action = "using 0.0" if optional else "rejecting sample"
                log.warning("ND45 %s over range/invalid (%r), %s", key, si, action)
                if overrange_seen is not None:
                    overrange_seen.add(key)
            if optional:
                si = 0.0
            else:
                invalid_critical.append(key)
        elif overrange_seen is not None and key in overrange_seen:
            overrange_seen.discard(key)
            log.info("ND45 %s back in range", key)
        values[key] = si

    if invalid_critical:
        raise PollError(
            "ND45 invalid critical value(s): " + ", ".join(invalid_critical)
        )
```

Leave `compute_derived(values)` after the new error check.

- [ ] **Step 5: Run focused and related tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_poller.py tests/test_app.py -q
```

Expected: all tests pass; invalid critical samples never reach `on_update`.

- [ ] **Step 6: Commit Task 1**

```powershell
git add -- src/nd45_dtsu666/nd45_poller.py tests/test_poller.py
git commit -m "fix: reject invalid critical ND45 samples"
```

---

### Task 2: Enforce finite float32 and stage datastore writes

**Files:**
- Modify: `tests/test_codec.py:1-75`
- Modify: `tests/test_server.py:1-240`
- Modify: `tests/test_static_debug.py:1-100`
- Modify: `src/nd45_dtsu666/codec.py:1-45`
- Modify: `src/nd45_dtsu666/dtsu_server.py:190-220`
- Modify: `src/nd45_dtsu666/static_debug.py:58-105`

**Interfaces:**
- Consumes: `encode_point(...)`, `update_datastore(...)`, and `build_static_pipeline(...)`.
- Produces: `FLOAT32_MAX: float`; `float_to_registers(...)` raises `ValueError` for every non-finite or out-of-range value; `update_datastore()` performs encode-first staging.

- [ ] **Step 1: Replace permissive codec tests with failing finite-only tests**

Replace `test_roundtrip_nan_and_inf` and
`test_float_to_registers_saturates_out_of_range_instead_of_raising` with:

```python
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("word_order", ["big", "little"])
@pytest.mark.parametrize("byte_order", ["big", "little"])
def test_float_to_registers_rejects_non_finite(value, word_order, byte_order):
    with pytest.raises(ValueError, match="finite float32"):
        float_to_registers(value, word_order, byte_order)


@pytest.mark.parametrize("value", [1e39, -1e39])
def test_float_to_registers_rejects_finite_float32_overflow(value):
    with pytest.raises(ValueError, match="finite float32"):
        float_to_registers(value)


def test_float_to_registers_accepts_float32_max():
    regs = float_to_registers(FLOAT32_MAX)
    assert registers_to_float(regs) == FLOAT32_MAX


def test_encode_point_rejects_overflow_created_by_scaling():
    with pytest.raises(ValueError, match="finite float32"):
        encode_point(
            1e38,
            scale=10.0,
            sign=1,
            offset=0.0,
            word_order="big",
            byte_order="big",
        )
```

Import `FLOAT32_MAX`.

- [ ] **Step 2: Add a failing test for encode-first datastore staging**

In `tests/test_server.py` add:

```python
def test_update_datastore_validation_failure_preserves_previous_image():
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    update_datastore(
        context,
        1,
        {"u_l1": 111.0, "p_total": 222.0},
        target,
    )
    before_u = context[1].getValues(3, target.points["u_l1"].addr, count=2)
    before_p = context[1].getValues(3, target.points["p_total"].addr, count=2)

    with pytest.raises(ValueError, match="finite float32"):
        update_datastore(
            context,
            1,
            {"u_l1": 230.0, "p_total": 1e38},
            target,
        )

    assert context[1].getValues(
        3, target.points["u_l1"].addr, count=2
    ) == before_u
    assert context[1].getValues(
        3, target.points["p_total"].addr, count=2
    ) == before_p
```

- [ ] **Step 3: Add a failing static-debug startup validation test**

In `tests/test_static_debug.py` add:

```python
def test_static_pipeline_rejects_unencodable_value_during_build():
    config = load_config("config/config.json")
    config.static_debug.values["u_l1"] = 1e38
    registers = load_registers("config/registers.json")

    with pytest.raises(ValueError, match="finite float32"):
        build_static_pipeline(
            config, registers, asyncio.Event(), RtuActivity()
        )
```

- [ ] **Step 4: Run the focused tests and verify RED**

Run:

```powershell
python -m pytest tests/test_codec.py tests/test_server.py tests/test_static_debug.py -q
```

Expected: codec tests fail because specials/overflow are encoded, the
datastore test shows `u_l1` was partially changed, and static pipeline build
does not raise.

- [ ] **Step 5: Implement the finite-binary32 codec contract**

In `src/nd45_dtsu666/codec.py`, add:

```python
FLOAT32_MAX = 3.4028234663852886e38
```

Replace `float_to_registers()` packing logic with:

```python
def float_to_registers(
    value: float,
    word_order: str = "big",
    byte_order: str = "big",
) -> list[int]:
    if not math.isfinite(value) or abs(value) > FLOAT32_MAX:
        raise ValueError(f"value is not representable as finite float32: {value!r}")
    raw = struct.pack(">f", value)
    hi, lo = raw[0:2], raw[2:4]
    words = [hi, lo] if word_order == "big" else [lo, hi]
    return [_bytes_word(w, byte_order) for w in words]
```

- [ ] **Step 6: Stage every encoding before the first datastore write**

Replace the write loop in `update_datastore()` with:

```python
    slave = context[slave_id]
    pending: list[tuple[int, int, list[int]]] = []
    for side in _targets(target):
        wo, bo = side.word_order, side.byte_order
        for pt in side.points.values():
            si = canonical.get(pt.from_)
            if si is None:
                continue
            if pt.divide_by_ct:
                si = si / ct_ratio
            regs = encode_point(si, pt.scale, pt.sign, pt.offset, wo, bo)
            pending.append((side.function_code, pt.addr, regs))

    for function_code, address, registers in pending:
        slave.setValues(function_code, address, registers)
```

Update the docstring to say all values are encoded before writes begin.

- [ ] **Step 7: Add synchronous static-debug preflight**

Immediately after `values = expand_static_values(...)` in
`build_static_pipeline()` add:

```python
    update_datastore(
        context,
        config.dtsu.slave_id,
        values,
        targets,
        ct_ratio=config.dtsu.identity.ir_at,
    )
```

This deliberately seeds the same deterministic register image that the feeder
will refresh.

- [ ] **Step 8: Run focused and pipeline tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_codec.py tests/test_server.py tests/test_static_debug.py tests/test_app.py -q
```

Expected: all tests pass; no test decodes a non-finite output value.

- [ ] **Step 9: Commit Task 2**

```powershell
git add -- src/nd45_dtsu666/codec.py src/nd45_dtsu666/dtsu_server.py src/nd45_dtsu666/static_debug.py tests/test_codec.py tests/test_server.py tests/test_static_debug.py
git commit -m "fix: reject unrepresentable DTSU float values"
```

---

### Task 3: Serve forward active energy at all observed aliases

**Files:**
- Modify: `config/registers.json:65-85`
- Modify: `config/registers.json:134-152`
- Modify: `tests/test_config.py:70-110`
- Modify: `tests/test_server.py:178-225`
- Modify: `tests/test_rtudebug.py:50-65`
- Modify: `docs/register-map.md:90-165`
- Modify: `docs/register-map.md:208-216`

**Interfaces:**
- Consumes: canonical `imp_energy_total` in kWh and `TargetPoint.divide_by_ct`.
- Produces: target points `forward_active_ep` at FC03 `0x100A` and FC04 `0x180A`, plus `forward_active_ep_alias` at FC03 `0x1050`.

- [ ] **Step 1: Add failing configuration assertions**

In the `ct_divided` set in `tests/test_config.py`, add:

```python
        "forward_active_ep", "forward_active_ep_alias",
```

Extend `test_load_registers_reads_sigen_ext_energy_map()` with:

```python
    classic = reg.dtsu_target.points["forward_active_ep"]
    alias = reg.dtsu_target.points["forward_active_ep_alias"]
    extended = energy.points["forward_active_ep"]
    assert classic.addr == 0x100A
    assert alias.addr == 0x1050
    assert extended.addr == 0x180A
    assert classic.from_ == alias.from_ == extended.from_ == "imp_energy_total"
    assert classic.divide_by_ct is True
    assert alias.divide_by_ct is True
    assert extended.divide_by_ct is False
```

- [ ] **Step 2: Add failing register-value and gap assertions**

Replace `test_sigen_ext_energy_gaps_return_zero_without_illegal_address()` with
a test that keeps validation coverage but narrows the zero-only part:

```python
def test_sigen_ext_energy_active_reads_are_valid_and_unknown_gap_is_zero():
    registers = load_registers("config/registers.json")
    targets = [
        registers.dtsu_target,
        registers.dtsu_sigen_ext_target,
        registers.dtsu_sigen_ext_energy,
    ]
    context = build_context(
        targets, slave_id=1, sigen_identity=registers.dtsu_sigen_identity
    )
    slave = context[1]

    assert slave.validate(4, 0x180A, count=22)
    assert slave.validate(4, 0x1828, count=4)
    assert slave.getValues(4, 0x180C, count=18) == [0] * 18
```

Extend `test_sigen_ext_energy_encodes_primary_kwh_at_offset_0x800()`:

```python
    forward_classic = registers.dtsu_target.points["forward_active_ep"]
    forward_alias = registers.dtsu_target.points["forward_active_ep_alias"]
    forward_extended = registers.dtsu_sigen_ext_energy.points["forward_active_ep"]

    for point in (forward_classic, forward_alias):
        regs = context[1].getValues(3, point.addr, count=2)
        assert registers_to_float(regs, "big", "big") == pytest.approx(
            1234.5 / 200
        )
    regs = context[1].getValues(4, forward_extended.addr, count=2)
    assert registers_to_float(regs, "big", "big") == pytest.approx(1234.5)
```

- [ ] **Step 3: Update the expected debug lookup and verify RED**

In `tests/test_rtudebug.py`, change the `0x180A`/22 assertion to:

```python
    assert idx.lookup(0x180A, 22, function_code=4) == [
        "forward_active_ep",
        "imp_ep",
    ]
```

Run:

```powershell
python -m pytest tests/test_config.py tests/test_server.py tests/test_rtudebug.py -q
```

Expected: failures report missing `forward_active_ep` and
`forward_active_ep_alias`.

- [ ] **Step 4: Add the three configuration mappings**

In `dtsu_target.points` in `config/registers.json`, add before the existing
`imp_ep` entries:

```json
      "forward_active_ep":       {"addr": 4106, "from": "imp_energy_total", "scale": 1, "divide_by_ct": true},
      "forward_active_ep_alias": {"addr": 4176, "from": "imp_energy_total", "scale": 1, "divide_by_ct": true},
```

In `dtsu_sigen_ext_energy.points`, add:

```json
      "forward_active_ep": {"addr": 6154, "from": "imp_energy_total", "scale": 1},
```

- [ ] **Step 5: Correct the register documentation**

In `docs/register-map.md`:

- add FC03 rows for `0x100A` and `0x1050`, naming both “forward active total”,
  sourced from `imp_energy_total`, secondary-side `/CT`;
- add FC04 `0x180A` to the energy table, naming it “forward active total”,
  sourced from `imp_energy_total`, primary-side;
- state that only `0x180C` through `0x181D` remain zero-filled in the first
  Sigen read before the pre-existing `0x181E` point;
- delete the known-gap bullet that called `0x180A` reactive energy.

Use this replacement wording:

```markdown
- **Energia czynna forward** (`0x180A` FC04 / `0x100A`, `0x1050` FC03) —
  odwzorowana z `imp_energy_total` ND45. FC04 podaje stronę pierwotną, a oba
  aliasy FC03 stronę wtórną (`/CT`), zgodnie ze skanem licznika.
```

- [ ] **Step 6: Run register and documentation-adjacent tests**

Run:

```powershell
python -m pytest tests/test_config.py tests/test_server.py tests/test_rtudebug.py tests/test_app.py tests/test_static_debug.py -q
```

Expected: all tests pass; `0x180A` is populated only after a canonical update,
while the unconfirmed surrounding gap remains zero.

- [ ] **Step 7: Commit Task 3**

```powershell
git add -- config/registers.json docs/register-map.md tests/test_config.py tests/test_server.py tests/test_rtudebug.py
git commit -m "feat: serve forward active energy at Sigen aliases"
```

---

### Task 4: Full regression verification

**Files:**
- Verify: all tracked project files

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: evidence that the complete converter suite and Python sources pass.

- [ ] **Step 1: Run the complete test suite**

Run:

```powershell
python -m pytest -q
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 2: Compile all Python sources and tests**

Run:

```powershell
python -m compileall -q src tests
```

Expected: exit code `0` and no output.

- [ ] **Step 3: Check formatting hazards and review the final diff**

Run:

```powershell
git diff --check HEAD~3
git status --short --branch
git log -4 --oneline
```

Expected: `git diff --check` has no output; the working tree is clean; history
contains the design and plan commits plus the three focused implementation
commits.

- [ ] **Step 4: Inspect final register values with the existing tests as executable evidence**

Run:

```powershell
python -m pytest tests/test_server.py::test_sigen_ext_energy_encodes_primary_kwh_at_offset_0x800 tests/test_poller.py::test_run_poller_does_not_publish_invalid_critical_sample tests/test_codec.py::test_encode_point_rejects_overflow_created_by_scaling -vv
```

Expected: exactly three selected tests pass.
