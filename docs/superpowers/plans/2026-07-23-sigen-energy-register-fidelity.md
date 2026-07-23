# Sigen Energy Register Fidelity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every confirmed FC03/FC04 energy register match the physical Sigenergy TPX-CH/DTSU666 meter under import and export, and make every independent simulated output configurable in `config/config.json`.

**Architecture:** Extend the existing ND45 energy poll to include all four hardware reactive-energy quadrants and derive the physical meter's directional aliases in the canonical model. Keep all target behavior declarative in `registers.json`, adding only one target encoder option for the physical meter's coarse high-word-only aliases; live and static modes continue to share the same target map and datastore update path.

**Tech Stack:** Python 3.11+, Pydantic 2, pymodbus 3.6.9, pytest/pytest-asyncio, JSON register/config maps.

## Global Constraints

- Read ND45 energy registers `900`-`995` as one 96-register Modbus block; do not add multiple reactive-energy requests.
- Calculate `reactive_energy_total` only from ND45 hardware counters `944/946`, `960/962`, `976/978`, and `992/994`; do not integrate instantaneous `Q`.
- Keep `0x153C`-`0x1540` and `0x2032`-`0x2036` outside this change.
- Treat invalid reactive-energy components as critical and reject the complete poll.
- Preserve atomic datastore updates: encode every target before writing the first register.
- Keep `0x1028`-`0x102E` and `0x182A`-`0x182E` constant zero.
- Derive `active_energy_total`, `net_imp_energy_total`, and `net_exp_energy_total`; do not expose them as independent static-debug inputs.
- Apply coarse low-word-zero encoding only to `0x1000`, `0x100A`, `0x1050`, `0x1800`, `0x180A`, and `0x1850`.
- The checked-in static example must model export with negative active power/PF and non-zero exported energy.
- Do not alter existing measurement, identity, fail-safe, transport, or apparent-power behavior.

## File Structure

- `config/registers.json`: ND45 reactive source composition and explicit FC03/FC04 energy mappings.
- `config/config.json`: complete reverse-flow static-debug example.
- `src/nd45_dtsu666/nd45_poller.py`: expanded energy read and directional derived values.
- `src/nd45_dtsu666/config.py`: `TargetPoint.zero_low_word` and allowed independent static values.
- `src/nd45_dtsu666/codec.py`: word-order-aware coarse float encoding.
- `src/nd45_dtsu666/dtsu_server.py`: pass the target encoding option without weakening atomic writes.
- `tests/test_poller.py`: four-quadrant composition, derived values, invalid-source handling.
- `tests/test_diagnostics.py`: self-test expectations for the new derived semantics.
- `tests/test_codec.py`: exact coarse encoding across word/byte orders.
- `tests/test_config.py`: explicit source/target map and static-config contracts.
- `tests/test_server.py`: physical energy map conformance at CT ratio 200.
- `tests/test_rtudebug.py`: names for the corrected Sigen query blocks.
- `tests/test_static_debug.py`: end-to-end reverse-flow simulation.
- `README.md`: operator-facing description of energy registers and static mode.
- `docs/register-map.md`: authoritative register tables and scan evidence.

---

### Task 1: Read Four-Quadrant Reactive Energy and Correct Derived Semantics

**Files:**
- Modify: `tests/test_poller.py:8-175`
- Modify: `tests/test_diagnostics.py:30-40`
- Modify: `config/registers.json:32-39`
- Modify: `src/nd45_dtsu666/nd45_poller.py:14-39`

**Interfaces:**
- Consumes: existing `SourcePoint.compose`, `compose(values, factors)`, and critical-value validation in `poll_once`.
- Produces: canonical `reactive_energy_total: float`, `active_energy_total: float`, `net_imp_energy_total: float`, and `net_exp_energy_total: float`.

- [ ] **Step 1: Write failing tests for the extended source image and directional aliases**

In `tests/test_poller.py`, add `compute_derived` to the import from
`nd45_dtsu666.nd45_poller`, then update
`test_poll_once_decodes_points` with these reactive source values:

```python
        944: 1.0, 946: 10.0,    # imported inductive -> 1010 kvarh
        960: 0.0, 962: 20.0,    # exported inductive -> 20 kvarh
        976: 2.0, 978: 30.0,    # imported capacitive -> 2030 kvarh
        992: 0.0, 994: 40.0,    # exported capacitive -> 40 kvarh
```

Replace its old `net_*` assertions and add the new totals:

```python
    assert values["reactive_energy_total"] == pytest.approx(3100.0, rel=1e-5)
    assert values["active_energy_total"] == pytest.approx(2357.0, rel=1e-5)
    assert values["net_imp_energy_total"] == pytest.approx(2345.0, rel=1e-5)
    assert values["net_exp_energy_total"] == pytest.approx(12.0, rel=1e-5)
```

Add a focused unit test:

```python
def test_compute_derived_matches_physical_directional_aliases():
    values = {
        "imp_energy_total": 7.0078125,
        "exp_energy_total": 0.19921875,
    }

    compute_derived(values)

    assert values["active_energy_total"] == pytest.approx(7.20703125)
    assert values["net_imp_energy_total"] == pytest.approx(7.0078125)
    assert values["net_exp_energy_total"] == pytest.approx(0.19921875)
```

Extend the parameter list in
`test_poll_once_rejects_invalid_critical_value`:

```python
        (944, 3e20, "reactive_energy_total"),
```

In `tests/test_diagnostics.py`, replace the two old arithmetic-net
assertions:

```python
    assert values["active_energy_total"] == pytest.approx(1234.5 + 67.8)
    assert values["net_imp_energy_total"] == pytest.approx(1234.5)
    assert values["net_exp_energy_total"] == pytest.approx(67.8)
```

- [ ] **Step 2: Run the focused tests and confirm the old behavior fails**

Run:

```powershell
python -m pytest tests/test_poller.py::test_poll_once_decodes_points tests/test_poller.py::test_compute_derived_matches_physical_directional_aliases tests/test_poller.py::test_poll_once_rejects_invalid_critical_value tests/test_diagnostics.py::test_synthetic_values_cover_every_dtsu_target_source -v
```

Expected: FAIL because `reactive_energy_total` and
`active_energy_total` do not exist and `net_*` still uses subtraction.

- [ ] **Step 3: Add the ND45 source composition and expand its read block**

In `config/registers.json`, append this point to `nd45_source.points`:

```json
      "reactive_energy_total": {
        "compose": [944, 946, 960, 962, 976, 978, 992, 994],
        "factors": [1000, 1, 1000, 1, 1000, 1, 1000, 1]
      }
```

In `src/nd45_dtsu666/nd45_poller.py`, replace the energy read group and
comment:

```python
# Fixed read blocks (base_addr, register_count) covering every mapped ND45 point.
# Group 1: measurements 50..145; Group 2: frequency 818..819;
# Group 3: active and four-quadrant reactive energy 900..995.
READ_GROUPS: list[tuple[int, int]] = [(50, 96), (818, 2), (900, 96)]
```

Replace `compute_derived` with:

```python
def compute_derived(values: dict[str, float]) -> None:
    """Fill canonical physical-meter energy aliases in place."""
    imp = values.get("imp_energy_total", 0.0)
    exp = values.get("exp_energy_total", 0.0)
    values["active_energy_total"] = imp + exp
    values["net_imp_energy_total"] = imp
    values["net_exp_energy_total"] = exp
```

The generic compose loop in `poll_once` already validates each component and
produces a single critical point named `reactive_energy_total`; do not add a
second manual summation path.

- [ ] **Step 4: Run poller and diagnostics tests**

Run:

```powershell
python -m pytest tests/test_poller.py tests/test_diagnostics.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the canonical source change**

```powershell
git add -- config/registers.json src/nd45_dtsu666/nd45_poller.py tests/test_poller.py tests/test_diagnostics.py
git commit -m "fix: derive Sigen energy totals from ND45 counters"
```

---

### Task 2: Add Word-Order-Aware Coarse Float Encoding

**Files:**
- Modify: `tests/test_codec.py:34-90`
- Modify: `tests/test_config.py:186-191`
- Modify: `src/nd45_dtsu666/config.py:80-93`
- Modify: `src/nd45_dtsu666/codec.py:48-53`
- Modify: `src/nd45_dtsu666/dtsu_server.py:190-224`

**Interfaces:**
- Consumes: `TargetPoint` scaling/CT metadata and existing atomic pending-write list.
- Produces: `TargetPoint.zero_low_word: bool` and
  `encode_point(..., zero_low_word: bool = False) -> list[int]`.

- [ ] **Step 1: Write failing codec and configuration tests**

Append to `tests/test_codec.py`:

```python
@pytest.mark.parametrize(
    ("word_order", "low_index"),
    [("big", 1), ("little", 0)],
)
@pytest.mark.parametrize("byte_order", ["big", "little"])
def test_encode_point_can_zero_logical_low_word(
    word_order, low_index, byte_order
):
    regs = encode_point(
        12.345,
        scale=1.0,
        sign=1,
        offset=0.0,
        word_order=word_order,
        byte_order=byte_order,
        zero_low_word=True,
    )

    assert regs[low_index] == 0
    assert regs[1 - low_index] != 0


def test_encode_point_preserves_low_word_by_default():
    assert encode_point(
        12.345,
        scale=1.0,
        sign=1,
        offset=0.0,
        word_order="big",
        byte_order="big",
    ) == float_to_registers(12.345, "big", "big")
```

In `tests/test_config.py`, import `TargetPoint` and extend
`test_target_point_defaults`:

```python
    assert pf.zero_low_word is False
    assert TargetPoint(addr=1, **{"from": "x"}).zero_low_word is False
```

- [ ] **Step 2: Run the focused tests and confirm the missing option**

Run:

```powershell
python -m pytest tests/test_codec.py::test_encode_point_can_zero_logical_low_word tests/test_codec.py::test_encode_point_preserves_low_word_by_default tests/test_config.py::test_target_point_defaults -v
```

Expected: FAIL because `encode_point` rejects `zero_low_word` and
`TargetPoint` has no such field.

- [ ] **Step 3: Implement the target option and codec behavior**

In `src/nd45_dtsu666/config.py`, add this field to `TargetPoint`:

```python
    # Physical TPX-CH coarse energy aliases expose only the IEEE754 high word.
    zero_low_word: bool = False
```

In `src/nd45_dtsu666/codec.py`, replace `encode_point` with:

```python
def encode_point(
    si: float,
    scale: float,
    sign: int,
    offset: float,
    word_order: str,
    byte_order: str,
    zero_low_word: bool = False,
) -> list[int]:
    register_float = si * sign * scale + offset
    registers = float_to_registers(register_float, word_order, byte_order)
    if zero_low_word:
        low_index = 1 if word_order == "big" else 0
        registers[low_index] = 0
    return registers
```

In `src/nd45_dtsu666/dtsu_server.py`, pass the new property in the existing
pre-write encoding loop:

```python
            regs = encode_point(
                si,
                pt.scale,
                pt.sign,
                pt.offset,
                wo,
                bo,
                zero_low_word=pt.zero_low_word,
            )
            pending.append((side.function_code, pt.addr, regs))
```

Do not move `setValues` into this loop; all encodes must still finish before
the first datastore mutation.

- [ ] **Step 4: Run codec, config, and atomicity tests**

Run:

```powershell
python -m pytest tests/test_codec.py tests/test_config.py tests/test_server.py::test_update_datastore_validation_failure_preserves_previous_image -q
```

Expected: PASS.

- [ ] **Step 5: Commit the reusable encoder option**

```powershell
git add -- src/nd45_dtsu666/config.py src/nd45_dtsu666/codec.py src/nd45_dtsu666/dtsu_server.py tests/test_codec.py tests/test_config.py
git commit -m "feat: support coarse TPX-CH float aliases"
```

---

### Task 3: Replace the FC03 and FC04 Energy Maps with Physical Behavior

**Files:**
- Modify: `tests/test_config.py:76-115`
- Modify: `tests/test_server.py:210-263`
- Modify: `tests/test_rtudebug.py:56-64`
- Modify: `config/registers.json:72-84`
- Modify: `config/registers.json:136-151`

**Interfaces:**
- Consumes: canonical values from Task 1 and `zero_low_word` from Task 2.
- Produces: explicit physical FC03/FC04 energy points and zero-filled
  unsupported phase-export ranges.

- [ ] **Step 1: Replace offset assumptions with explicit map contract tests**

In `tests/test_config.py`, replace
`test_load_registers_reads_sigen_ext_energy_map` with:

```python
def test_load_registers_reads_physical_energy_maps():
    reg = load_registers("config/registers.json")
    classic = reg.dtsu_target.points
    extended = reg.dtsu_sigen_ext_energy.points

    classic_expected = {
        "imp_energy_coarse": (0x1000, "imp_energy_total", True),
        "reactive_energy_coarse": (0x100A, "reactive_energy_total", True),
        "imp_ep": (0x101E, "imp_energy_total", False),
        "imp_ep_l1": (0x1020, "imp_energy_l1", False),
        "imp_ep_l2": (0x1022, "imp_energy_l2", False),
        "imp_ep_l3": (0x1024, "imp_energy_l3", False),
        "net_imp_ep": (0x1026, "net_imp_energy_total", False),
        "net_exp_ep": (0x1030, "net_exp_energy_total", False),
        "reactive_energy_coarse_alias": (
            0x1050, "reactive_energy_total", True
        ),
    }
    extended_expected = {
        "active_energy_coarse": (0x1800, "active_energy_total", True),
        "reactive_energy_coarse": (0x180A, "reactive_energy_total", True),
        "imp_ep": (0x181E, "imp_energy_total", False),
        "imp_ep_l1": (0x1820, "imp_energy_l1", False),
        "imp_ep_l2": (0x1822, "imp_energy_l2", False),
        "imp_ep_l3": (0x1824, "imp_energy_l3", False),
        "net_imp_ep": (0x1826, "net_imp_energy_total", False),
        "exp_ep": (0x1828, "exp_energy_total", False),
        "net_exp_ep": (0x1830, "net_exp_energy_total", False),
        "reactive_energy_coarse_alias": (
            0x1850, "reactive_energy_total", True
        ),
    }

    assert set(classic_expected) <= set(classic)
    assert set(extended) == set(extended_expected)
    for name, (addr, source, coarse) in classic_expected.items():
        point = classic[name]
        assert (point.addr, point.from_, point.zero_low_word) == (
            addr, source, coarse
        )
        assert point.divide_by_ct is True
    for name, (addr, source, coarse) in extended_expected.items():
        point = extended[name]
        assert (point.addr, point.from_, point.zero_low_word) == (
            addr, source, coarse
        )
        assert point.divide_by_ct is False

    assert not {"exp_ep", "exp_ep_l1", "exp_ep_l2", "exp_ep_l3"} & set(classic)
    assert not {"exp_ep_l1", "exp_ep_l2", "exp_ep_l3"} & set(extended)
```

Update `test_load_registers_classic_secondary_side_points_divide_by_ct` so
the energy names in `ct_divided` are:

```python
        "imp_energy_coarse", "reactive_energy_coarse",
        "reactive_energy_coarse_alias",
        "imp_ep", "imp_ep_l1", "imp_ep_l2", "imp_ep_l3",
        "net_imp_ep", "net_exp_ep",
```

- [ ] **Step 2: Write a physical reverse-flow datastore test**

In `tests/test_server.py`, add `float_to_registers` to the codec import and
replace `test_sigen_ext_energy_encodes_primary_kwh_at_offset_0x800` with:

```python
def _coarse_float(value: float) -> list[int]:
    registers = float_to_registers(value, "big", "big")
    registers[1] = 0
    return registers


def test_energy_maps_match_physical_reverse_flow_scan():
    registers = load_registers("config/registers.json")
    targets = [
        registers.dtsu_target,
        registers.dtsu_sigen_ext_target,
        registers.dtsu_sigen_ext_energy,
    ]
    context = build_context(targets, slave_id=1)
    canonical = {
        "active_energy_total": 7.20703125,
        "reactive_energy_total": 2.796875,
        "imp_energy_total": 7.0078125,
        "imp_energy_l1": 2.0039122,
        "imp_energy_l2": 2.394534,
        "imp_energy_l3": 2.1914597,
        "net_imp_energy_total": 7.0078125,
        "exp_energy_total": 0.19921875,
        "net_exp_energy_total": 0.19921875,
    }
    update_datastore(context, 1, canonical, targets, ct_ratio=200)
    slave = context[1]

    assert slave.getValues(4, 0x1800, count=2) == _coarse_float(7.20703125)
    assert slave.getValues(4, 0x180A, count=2) == _coarse_float(2.796875)
    assert slave.getValues(4, 0x180C, count=18) == [0] * 18
    assert slave.getValues(4, 0x181E, count=2) == float_to_registers(7.0078125)
    assert slave.getValues(4, 0x1826, count=2) == float_to_registers(7.0078125)
    assert slave.getValues(4, 0x1828, count=2) == float_to_registers(0.19921875)
    assert slave.getValues(4, 0x182A, count=6) == [0] * 6
    assert slave.getValues(4, 0x1830, count=2) == float_to_registers(0.19921875)
    assert slave.getValues(4, 0x1850, count=2) == _coarse_float(2.796875)

    assert slave.getValues(3, 0x1000, count=2) == _coarse_float(
        7.0078125 / 200
    )
    assert slave.getValues(3, 0x100A, count=2) == _coarse_float(
        2.796875 / 200
    )
    assert slave.getValues(3, 0x101E, count=2) == float_to_registers(
        7.0078125 / 200
    )
    assert slave.getValues(3, 0x1026, count=2) == float_to_registers(
        7.0078125 / 200
    )
    assert slave.getValues(3, 0x1028, count=8) == [0] * 8
    assert slave.getValues(3, 0x1030, count=2) == float_to_registers(
        0.19921875 / 200
    )
    assert slave.getValues(3, 0x1050, count=2) == _coarse_float(
        2.796875 / 200
    )
```

Keep and update
`test_sigen_ext_energy_active_reads_are_valid_and_unknown_gap_is_zero` so it
still asserts:

```python
    assert slave.validate(4, 0x180A, count=22)
    assert slave.validate(4, 0x1828, count=4)
    assert slave.getValues(4, 0x180C, count=18) == [0] * 18
```

- [ ] **Step 3: Update RTU debug expectations**

In `tests/test_rtudebug.py`, replace the energy lookup test body:

```python
    assert idx.lookup(0x180A, 22, function_code=4) == [
        "reactive_energy_coarse",
        "imp_ep",
    ]
    assert idx.lookup(0x1828, 4, function_code=4) == ["exp_ep"]
```

- [ ] **Step 4: Run the map tests and confirm the current map fails**

Run:

```powershell
python -m pytest tests/test_config.py::test_load_registers_reads_physical_energy_maps tests/test_server.py::test_energy_maps_match_physical_reverse_flow_scan tests/test_server.py::test_sigen_ext_energy_active_reads_are_valid_and_unknown_gap_is_zero tests/test_rtudebug.py::test_lookup_sigen_ext_energy_points_at_offset_0x800 -v
```

Expected: FAIL because the old map labels `0x180A` as active import, maps
phase export, omits `0x1800`/`0x1850`, and computes different `net_*` values.

- [ ] **Step 5: Replace the energy point definitions**

In `config/registers.json`, replace the classic energy points with:

```json
      "imp_energy_coarse": {
        "addr": 4096, "from": "imp_energy_total", "scale": 1,
        "divide_by_ct": true, "zero_low_word": true
      },
      "reactive_energy_coarse": {
        "addr": 4106, "from": "reactive_energy_total", "scale": 1,
        "divide_by_ct": true, "zero_low_word": true
      },
      "imp_ep": {
        "addr": 4126, "from": "imp_energy_total", "scale": 1,
        "divide_by_ct": true
      },
      "imp_ep_l1": {
        "addr": 4128, "from": "imp_energy_l1", "scale": 1,
        "divide_by_ct": true
      },
      "imp_ep_l2": {
        "addr": 4130, "from": "imp_energy_l2", "scale": 1,
        "divide_by_ct": true
      },
      "imp_ep_l3": {
        "addr": 4132, "from": "imp_energy_l3", "scale": 1,
        "divide_by_ct": true
      },
      "net_imp_ep": {
        "addr": 4134, "from": "net_imp_energy_total", "scale": 1,
        "divide_by_ct": true
      },
      "net_exp_ep": {
        "addr": 4144, "from": "net_exp_energy_total", "scale": 1,
        "divide_by_ct": true
      },
      "reactive_energy_coarse_alias": {
        "addr": 4176, "from": "reactive_energy_total", "scale": 1,
        "divide_by_ct": true, "zero_low_word": true
      }
```

Replace `dtsu_sigen_ext_energy.points` with:

```json
      "active_energy_coarse": {
        "addr": 6144, "from": "active_energy_total", "scale": 1,
        "zero_low_word": true
      },
      "reactive_energy_coarse": {
        "addr": 6154, "from": "reactive_energy_total", "scale": 1,
        "zero_low_word": true
      },
      "imp_ep": {"addr": 6174, "from": "imp_energy_total", "scale": 1},
      "imp_ep_l1": {"addr": 6176, "from": "imp_energy_l1", "scale": 1},
      "imp_ep_l2": {"addr": 6178, "from": "imp_energy_l2", "scale": 1},
      "imp_ep_l3": {"addr": 6180, "from": "imp_energy_l3", "scale": 1},
      "net_imp_ep": {
        "addr": 6182, "from": "net_imp_energy_total", "scale": 1
      },
      "exp_ep": {"addr": 6184, "from": "exp_energy_total", "scale": 1},
      "net_exp_ep": {
        "addr": 6192, "from": "net_exp_energy_total", "scale": 1
      },
      "reactive_energy_coarse_alias": {
        "addr": 6224, "from": "reactive_energy_total", "scale": 1,
        "zero_low_word": true
      }
```

Do not create points for the confirmed zero ranges. The sequential datastore
will keep them zero while the later mapped addresses make the complete reads
valid.

- [ ] **Step 6: Run all register-map consumers**

Run:

```powershell
python -m pytest tests/test_config.py tests/test_server.py tests/test_rtudebug.py tests/test_app.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit the physical target map**

```powershell
git add -- config/registers.json tests/test_config.py tests/test_server.py tests/test_rtudebug.py
git commit -m "fix: match physical Sigen energy register map"
```

---

### Task 4: Make Static Debug Cover Every Independent Output

**Files:**
- Modify: `tests/test_config.py:132-169`
- Modify: `tests/test_static_debug.py:11-104`
- Modify: `src/nd45_dtsu666/config.py:12-52`
- Modify: `config/config.json:5-31`

**Interfaces:**
- Consumes: target maps from Task 3 and `compute_derived` from Task 1.
- Produces: a complete reverse-flow `static_debug.values` example and an
  allowlist containing only independent inputs.

- [ ] **Step 1: Write failing configuration contract tests**

Update `test_load_config_reads_seed` in `tests/test_config.py`:

```python
    assert cfg.static_debug.values["p_total"] == -60000.0
    assert cfg.static_debug.values["pf_total"] == -0.95
    assert cfg.static_debug.values["s_total"] == 60300.0
    assert cfg.static_debug.values["imp_energy_total"] == 7.0
    assert cfg.static_debug.values["exp_energy_total"] == 0.2
    assert cfg.static_debug.values["reactive_energy_total"] == 2.8
```

Add:

```python
def test_static_debug_accepts_total_reactive_energy():
    configured = StaticDebugConf(values={"reactive_energy_total": 2.8})
    assert configured.values["reactive_energy_total"] == 2.8


@pytest.mark.parametrize(
    "name",
    [
        "active_energy_total",
        "net_imp_energy_total",
        "net_exp_energy_total",
        "exp_energy_l1",
        "exp_energy_l2",
        "exp_energy_l3",
    ],
)
def test_static_debug_rejects_derived_or_constant_zero_energy_inputs(name):
    with pytest.raises(ValidationError, match="unknown static debug value"):
        StaticDebugConf(values={name: 1.0})
```

- [ ] **Step 2: Write an end-to-end reverse-flow static test**

In `tests/test_static_debug.py`, add:

```python
async def test_static_pipeline_serves_complete_reverse_flow_energy_image():
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    stop = asyncio.Event()
    pipe = build_static_pipeline(config, registers, stop, RtuActivity())
    slave = pipe.context[config.dtsu.slave_id]
    feeder = asyncio.create_task(pipe.coros[0])
    try:
        await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(feeder, timeout=1.0)
        values, _ = pipe.store.snapshot()
        assert values["p_total"] == -60000.0
        assert values["reactive_energy_total"] == 2.8
        assert values["active_energy_total"] == pytest.approx(7.2)
        assert values["net_imp_energy_total"] == pytest.approx(7.0)
        assert values["net_exp_energy_total"] == pytest.approx(0.2)

        assert registers_to_float(
            slave.getValues(4, 0x151C, count=2), "big", "big"
        ) == pytest.approx(-60.0)
        assert registers_to_float(
            slave.getValues(4, 0x1828, count=2), "big", "big"
        ) == pytest.approx(0.2)
        assert slave.getValues(4, 0x182A, count=6) == [0] * 6
        assert registers_to_float(
            slave.getValues(4, 0x1830, count=2), "big", "big"
        ) == pytest.approx(0.2)
    finally:
        stop.set()
        if not feeder.done():
            await asyncio.wait_for(feeder, timeout=1.0)
        pipe.coros[1].close()
```

Also update
`test_expand_static_values_preserves_configured_and_zero_fills_missing` so
its `required` set includes all three target maps:

```python
        for target in (
            registers.dtsu_target,
            registers.dtsu_sigen_ext_target,
            registers.dtsu_sigen_ext_energy,
        )
```

- [ ] **Step 3: Run the static tests and confirm the seed is incomplete**

Run:

```powershell
python -m pytest tests/test_config.py::test_load_config_reads_seed tests/test_config.py::test_static_debug_accepts_total_reactive_energy tests/test_config.py::test_static_debug_rejects_derived_or_constant_zero_energy_inputs tests/test_static_debug.py::test_static_pipeline_serves_complete_reverse_flow_energy_image -v
```

Expected: FAIL because the static allowlist lacks
`reactive_energy_total`, still accepts derived/unused keys, and the checked-in
example contains no energy or apparent-power values.

- [ ] **Step 4: Restrict the static allowlist to independent values**

In `src/nd45_dtsu666/config.py`, keep the existing measurement/import keys,
add:

```python
        "reactive_energy_total",
```

and remove:

```python
        "exp_energy_l1",
        "exp_energy_l2",
        "exp_energy_l3",
        "net_imp_energy_total",
        "net_exp_energy_total",
```

Do not add `active_energy_total`; `compute_derived` owns it.

- [ ] **Step 5: Replace the checked-in static values with a complete export example**

In `config/config.json`, replace `static_debug.values` with:

```json
    "values": {
      "u_l1": 9000.0,
      "u_l2": 9000.0,
      "u_l3": 9000.0,
      "u_l12": 15588.0,
      "u_l23": 15588.0,
      "u_l31": 15588.0,
      "i_l1": 10.0,
      "i_l2": 20.0,
      "i_l3": 30.0,
      "p_l1": -10000.0,
      "p_l2": -20000.0,
      "p_l3": -30000.0,
      "p_total": -60000.0,
      "q_l1": 1000.0,
      "q_l2": 2000.0,
      "q_l3": 3000.0,
      "q_total": 6000.0,
      "s_l1": 10050.0,
      "s_l2": 20100.0,
      "s_l3": 30150.0,
      "s_total": 60300.0,
      "pf_l1": -0.95,
      "pf_l2": -0.95,
      "pf_l3": -0.95,
      "pf_total": -0.95,
      "freq": 50.0,
      "imp_energy_total": 7.0,
      "imp_energy_l1": 2.0,
      "imp_energy_l2": 2.4,
      "imp_energy_l3": 2.6,
      "exp_energy_total": 0.2,
      "reactive_energy_total": 2.8
    }
```

- [ ] **Step 6: Run configuration and static pipeline tests**

Run:

```powershell
python -m json.tool config/config.json > $null
python -m pytest tests/test_config.py tests/test_static_debug.py -q
```

Expected: valid JSON and all tests PASS.

- [ ] **Step 7: Commit the complete simulator configuration**

```powershell
git add -- config/config.json src/nd45_dtsu666/config.py tests/test_config.py tests/test_static_debug.py
git commit -m "feat: simulate complete Sigen reverse-flow image"
```

---

### Task 5: Update Operator Documentation and Run Full Regression

**Files:**
- Modify: `README.md:7-21`
- Modify: `README.md:90-106`
- Modify: `docs/register-map.md:21-43`
- Modify: `docs/register-map.md:93-110`
- Modify: `docs/register-map.md:147-169`
- Modify: `docs/register-map.md:199-223`

**Interfaces:**
- Consumes: final source, target, and static-debug contracts from Tasks 1-4.
- Produces: operator documentation that exactly matches runtime behavior and
  physical scan evidence.

- [ ] **Step 1: Replace the obsolete README energy description**

Rewrite the introductory energy paragraph to state:

```markdown
The Sigen FC04 energy map reproduces the physical TPX-CH behavior rather than
assuming a uniform `+0x800` copy of the generic DTSU666 map. `0x180A` is the
coarse total reactive-energy accumulator built from all four ND45 reactive
quadrants; `0x181E` is precise active import; `0x1828` is precise active
export. The classic FC03 energy aliases are CT-side values. Confirmed
phase-export fields remain zero, while the directional `net_*` fields repeat
their corresponding import/export totals as on the physical meter.
```

In the static-mode section, document the independent configurable categories
and add:

```markdown
The checked-in example represents export: active power and PF are negative,
`exp_energy_total` is non-zero, and `reactive_energy_total` drives the coarse
reactive aliases. `active_energy_total`, both `net_*` values, CT-side values,
and coarse aliases are derived automatically. Confirmed zero-only phase
export registers cannot be configured.
```

- [ ] **Step 2: Rewrite the canonical and energy tables**

In `docs/register-map.md`:

1. Add `reactive_energy_total` with the four ND45 pairs and the exact formula.
2. Replace arithmetic `net_*` formulas with directional copies.
3. Add `active_energy_total = imp_energy_total + exp_energy_total`.
4. Replace the FC03 energy table with the addresses from Task 3, explicitly
   marking `0x1028`-`0x102E` zero.
5. Replace the FC04 energy table with the addresses from Task 3, explicitly
   marking `0x182A`-`0x182E` zero.
6. Explain that the six coarse aliases zero the IEEE754 low word.
7. Add reverse-flow evidence:

```markdown
| FC04 Pt (`0x151C`) | -3.058557 kW |
| FC04 reactive coarse (`0x180A`) | 2.796875 kvarh |
| FC04 ImpEp (`0x181E`) | 7.0078125 kWh |
| FC04 ExpEp (`0x1828`) | 0.19921875 kWh |
| FC04 export alias (`0x1830`) | 0.19921875 kWh |
| FC04 phase export (`0x182A`-`0x182E`) | 0 |
```

8. Remove the resolved "export not verified" and sign-convention gap bullets.
9. Keep the angle registers in the known-gaps section because they remain
   deliberately outside this change.

- [ ] **Step 3: Run documentation and configuration consistency searches**

Run:

```powershell
rg -n "forward_active_ep|max\\(imp|ExpEp L1|0x180A.*active|eksport.*brak" README.md docs/register-map.md config/registers.json src tests
```

Expected: no obsolete production/test/documentation statements. Historical
files under `docs/superpowers/` may still describe their original designs and
must not be rewritten.

Run:

```powershell
python -m json.tool config/config.json > $null
python -m json.tool config/registers.json > $null
git diff --check
```

Expected: both JSON files parse and `git diff --check` produces no errors.

- [ ] **Step 4: Run the focused energy and simulation suite**

Run:

```powershell
python -m pytest tests/test_poller.py tests/test_codec.py tests/test_config.py tests/test_server.py tests/test_rtudebug.py tests/test_static_debug.py tests/test_diagnostics.py tests/test_app.py -q
```

Expected: PASS.

- [ ] **Step 5: Run the complete regression suite**

Run:

```powershell
python -m pytest -q
```

Expected: all tests PASS with no unclosed-coroutine warnings.

- [ ] **Step 6: Inspect the final diff and commit documentation**

Run:

```powershell
git diff --stat
git diff -- README.md docs/register-map.md
git status --short
```

Confirm that unrelated files and the local `tmp/` rendering directory are not
staged.

Commit:

```powershell
git add -- README.md docs/register-map.md
git commit -m "docs: document physical Sigen energy behavior"
```

- [ ] **Step 7: Verify the committed repository state**

Run:

```powershell
git status --short --branch
git log -6 --oneline
```

Expected: `main` contains the five implementation commits after the design
and plan commits; only the known local untracked `tmp/` directory may remain.
