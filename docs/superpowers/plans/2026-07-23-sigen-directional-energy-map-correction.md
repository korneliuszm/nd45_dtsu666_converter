# Sigen Directional Energy Map Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace zero-filled physical Sigenergy energy registers with active-export and directional-reactive values read from the ND45.

**Architecture:** Keep the existing single ND45 energy read block and declarative target maps. Split the former all-quadrant reactive source into independent `Q+` and `Q-` canonical points, then map total and per-phase active export plus the two reactive directions into both CT-side FC03 and primary-side FC04. Static mode continues to use the same canonical expansion and datastore update path.

**Tech Stack:** Python 3.11+, Pydantic 2, pymodbus 3.6.9, pytest/pytest-asyncio, JSON register/config maps.

## Global Constraints

- Keep the ND45 energy read as one 96-register block from `900` through `995`.
- Compose `reactive_imp_energy_total` only from `944/946` and `976/978`.
- Compose `reactive_exp_energy_total` only from `960/962` and `992/994`.
- Validate every composed component before summation and reject the complete poll on invalid data.
- Preserve `active_energy_total = imp_energy_total + exp_energy_total`.
- Preserve `net_imp_energy_total = imp_energy_total` and `net_exp_energy_total = exp_energy_total`.
- Divide every FC03 energy point by CT; do not divide FC04 energy points by CT.
- Zero the logical IEEE754 low word only for the ten coarse addresses listed in the design.
- Preserve encode-all-before-write datastore atomicity.
- Do not change measurement, apparent-power, identity, transport, fail-safe, watchdog, or sign behavior.
- Leave only genuinely unmapped gaps zero.

## File Structure

- `config/registers.json`: directional ND45 reactive compositions and complete FC03/FC04 energy maps.
- `src/nd45_dtsu666/config.py`: independent static-debug energy allowlist.
- `config/config.json`: scan-derived reverse-flow simulation values.
- `tests/test_poller.py`: directional composition and invalid-component rejection.
- `tests/test_config.py`: exact source, target, CT, coarse, and static contracts.
- `tests/test_server.py`: physical register-image conformance.
- `tests/test_rtudebug.py`: names returned for Sigen energy query blocks.
- `tests/test_static_debug.py`: end-to-end simulation of every corrected register.
- `tests/test_diagnostics.py`: synthetic source coverage after canonical renaming.
- `README.md`: exact configurable static keys and behavior.
- `docs/register-map.md`: authoritative corrected energy tables and scan evidence.

---

### Task 1: Add ND45 Reactive Energy Directions

**Files:**
- Modify: `tests/test_poller.py:91-206`
- Modify: `tests/test_diagnostics.py:29-42`
- Modify: `config/registers.json:40-43`

**Interfaces:**
- Consumes: `SourcePoint.compose`, component validation in `poll_once`, and the unchanged `READ_GROUPS`.
- Produces: `reactive_imp_energy_total: float` and `reactive_exp_energy_total: float`.

- [ ] **Step 1: Replace the aggregate assertions with directional source tests**

In `test_poll_once_decodes_points`, replace the aggregate assertion with:

```python
    assert values["reactive_imp_energy_total"] == pytest.approx(
        3040.0, rel=1e-5
    )
    assert values["reactive_exp_energy_total"] == pytest.approx(
        60.0, rel=1e-5
    )
```

The fixture already gives imported values `1010 + 2030` and exported values
`20 + 40`.

Replace the reactive invalid case with both directions:

```python
        (944, 3e20, "reactive_imp_energy_total"),
        (960, 3e20, "reactive_exp_energy_total"),
```

Replace `test_poll_once_rejects_invalid_composite_parts_that_cancel` with:

```python
@pytest.mark.parametrize(
    ("addresses", "point"),
    [
        ((944, 976), "reactive_imp_energy_total"),
        ((960, 992), "reactive_exp_energy_total"),
    ],
)
async def test_poll_once_rejects_invalid_directional_parts_that_cancel(
    addresses, point, caplog
):
    src = load_registers("config/registers.json").nd45_source
    image = {
        addresses[0]: _raw_float_registers(3e20),
        addresses[1]: _raw_float_registers(-3e20),
    }
    seen: set[str] = set()

    with caplog.at_level("WARNING", logger="nd45_dtsu666.nd45_poller"):
        for _ in range(2):
            with pytest.raises(PollError, match=point):
                await poll_once(
                    FakeClient(image), src, slave=1, overrange_seen=seen
                )

    warnings = [
        record for record in caplog.records if point in record.getMessage()
    ]
    assert len(warnings) == 1
```

Update `test_synthetic_values_cover_every_dtsu_target_source` so it asserts:

```python
    assert values["reactive_imp_energy_total"] == 0.0
    assert values["reactive_exp_energy_total"] == 0.0
```

- [ ] **Step 2: Run the focused RED tests**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_poller.py::test_poll_once_decodes_points tests/test_poller.py::test_poll_once_rejects_invalid_critical_value tests/test_poller.py::test_poll_once_rejects_invalid_directional_parts_that_cancel tests/test_diagnostics.py::test_synthetic_values_cover_every_dtsu_target_source -v
```

Expected: failures because the source map still exposes only
`reactive_energy_total`.

- [ ] **Step 3: Add the directional source compositions**

In `config/registers.json`, add these two source points alongside the existing
aggregate:

```json
      "reactive_imp_energy_total": {
        "compose": [944, 946, 976, 978],
        "factors": [1000, 1, 1000, 1]
      },
      "reactive_exp_energy_total": {
        "compose": [960, 962, 992, 994],
        "factors": [1000, 1, 1000, 1]
      }
```

Keep `reactive_energy_total` only as a transitional compatibility source in
Task 1 because the target map still consumes it. Task 2 removes it in the same
commit that switches all target registers to the directional sources.

Do not change `READ_GROUPS` or add manual composition code. The generic
component validator must remain the only validation path.

- [ ] **Step 4: Run poller and diagnostics suites**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_poller.py tests/test_diagnostics.py -q
```

Expected: all tests pass without warnings.

- [ ] **Step 5: Commit the source correction**

```powershell
git add -- config/registers.json tests/test_poller.py tests/test_diagnostics.py
git commit -m "fix: split ND45 reactive energy directions"
```

---

### Task 2: Replace Zero Energy Fields with the Physical FC03/FC04 Map

**Files:**
- Modify: `tests/test_config.py:77-145`
- Modify: `tests/test_server.py:218-287`
- Modify: `tests/test_rtudebug.py:56-64`
- Modify: `config/registers.json:77-112`
- Modify: `config/registers.json:169-190`

**Interfaces:**
- Consumes: Task 1 directional canonical values and existing
  `TargetPoint.zero_low_word`.
- Produces: complete physical energy target points in both Modbus spaces.

- [ ] **Step 1: Replace the exact target-map expectations**

Use these expected dictionaries in
`test_load_registers_reads_physical_energy_maps`:

```python
    classic_expected = {
        "active_energy_coarse": (0x1000, "active_energy_total", True),
        "reactive_exp_energy_coarse": (
            0x100A, "reactive_exp_energy_total", True
        ),
        "reactive_imp_energy_coarse": (
            0x1014, "reactive_imp_energy_total", True
        ),
        "imp_ep": (0x101E, "imp_energy_total", False),
        "imp_ep_l1": (0x1020, "imp_energy_l1", False),
        "imp_ep_l2": (0x1022, "imp_energy_l2", False),
        "imp_ep_l3": (0x1024, "imp_energy_l3", False),
        "net_imp_ep": (0x1026, "net_imp_energy_total", False),
        "exp_ep": (0x1028, "exp_energy_total", False),
        "exp_ep_l1": (0x102A, "exp_energy_l1", False),
        "exp_ep_l2": (0x102C, "exp_energy_l2", False),
        "exp_ep_l3": (0x102E, "exp_energy_l3", False),
        "net_exp_ep": (0x1030, "net_exp_energy_total", False),
        "reactive_imp_energy_coarse_alias": (
            0x103C, "reactive_imp_energy_total", True
        ),
        "reactive_exp_energy_coarse_alias": (
            0x1050, "reactive_exp_energy_total", True
        ),
    }
    extended_expected = {
        "active_energy_coarse": (0x1800, "active_energy_total", True),
        "reactive_exp_energy_coarse": (
            0x180A, "reactive_exp_energy_total", True
        ),
        "reactive_imp_energy_coarse": (
            0x1814, "reactive_imp_energy_total", True
        ),
        "imp_ep": (0x181E, "imp_energy_total", False),
        "imp_ep_l1": (0x1820, "imp_energy_l1", False),
        "imp_ep_l2": (0x1822, "imp_energy_l2", False),
        "imp_ep_l3": (0x1824, "imp_energy_l3", False),
        "net_imp_ep": (0x1826, "net_imp_energy_total", False),
        "exp_ep": (0x1828, "exp_energy_total", False),
        "exp_ep_l1": (0x182A, "exp_energy_l1", False),
        "exp_ep_l2": (0x182C, "exp_energy_l2", False),
        "exp_ep_l3": (0x182E, "exp_energy_l3", False),
        "net_exp_ep": (0x1830, "net_exp_energy_total", False),
        "reactive_imp_energy_coarse_alias": (
            0x183C, "reactive_imp_energy_total", True
        ),
        "reactive_exp_energy_coarse_alias": (
            0x1850, "reactive_exp_energy_total", True
        ),
    }
```

For FC04, assert `set(extended) == set(extended_expected)`. For FC03, define
the energy subset as points whose addresses are in `0x1000..0x1051`, then
assert that subset equals `set(classic_expected)`. Keep the existing loop that
checks `(addr, from_, zero_low_word)` and `/CT`.

Also assert:

```python
    assert "reactive_energy_total" not in reg.nd45_source.points
```

Update the `ct_divided` set to include every key in `classic_expected`.

- [ ] **Step 2: Replace the server image with latest-scan values**

Use this canonical fixture in
`test_energy_maps_match_physical_reverse_flow_scan`:

```python
    canonical = {
        "active_energy_total": 10.0,
        "reactive_exp_energy_total": 2.796875,
        "reactive_imp_energy_total": 1.1953125,
        "imp_energy_total": 7.0,
        "imp_energy_l1": 2.0,
        "imp_energy_l2": 2.390625,
        "imp_energy_l3": 2.1875,
        "net_imp_energy_total": 7.0,
        "exp_energy_total": 3.0,
        "exp_energy_l1": 0.796875,
        "exp_energy_l2": 1.0,
        "exp_energy_l3": 1.0,
        "net_exp_energy_total": 3.0,
    }
```

Assert all FC04 values:

```python
    assert slave.getValues(4, 0x1800, 2) == _coarse_float(10.0)
    assert slave.getValues(4, 0x180A, 2) == _coarse_float(2.796875)
    assert slave.getValues(4, 0x1814, 2) == _coarse_float(1.1953125)
    assert slave.getValues(4, 0x181E, 2) == float_to_registers(7.0)
    assert slave.getValues(4, 0x1828, 2) == float_to_registers(3.0)
    assert slave.getValues(4, 0x182A, 2) == float_to_registers(0.796875)
    assert slave.getValues(4, 0x182C, 2) == float_to_registers(1.0)
    assert slave.getValues(4, 0x182E, 2) == float_to_registers(1.0)
    assert slave.getValues(4, 0x1830, 2) == float_to_registers(3.0)
    assert slave.getValues(4, 0x183C, 2) == _coarse_float(1.1953125)
    assert slave.getValues(4, 0x1850, 2) == _coarse_float(2.796875)
```

Assert the same FC03 values divided by `200`, including every address from
`0x1028` through `0x102E`. Update the gap assertion to:

```python
    assert slave.getValues(4, 0x180C, count=8) == [0] * 8
    assert slave.getValues(4, 0x1816, count=8) == [0] * 8
```

- [ ] **Step 3: Update RTU lookup expectations**

```python
    assert idx.lookup(0x180A, 22, function_code=4) == [
        "reactive_exp_energy_coarse",
        "reactive_imp_energy_coarse",
        "imp_ep",
    ]
    assert idx.lookup(0x1828, 8, function_code=4) == [
        "exp_ep",
        "exp_ep_l1",
        "exp_ep_l2",
        "exp_ep_l3",
    ]
```

- [ ] **Step 4: Run the focused RED tests**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_config.py::test_load_registers_reads_physical_energy_maps tests/test_server.py::test_energy_maps_match_physical_reverse_flow_scan tests/test_server.py::test_sigen_ext_energy_active_reads_are_valid_and_unknown_gap_is_zero tests/test_rtudebug.py::test_lookup_sigen_ext_energy_points_at_offset_0x800 -v
```

Expected: failures because the map still uses import at `0x1000`, lacks
directional reactive points, and zero-fills phase export.

- [ ] **Step 5: Replace both target maps**

First remove the transitional `reactive_energy_total` source point so the
source and target map change atomically.

In `config/registers.json`, create exactly the points listed in
`classic_expected` and `extended_expected`. Every classic point uses
`"divide_by_ct": true`. The ten points marked coarse use
`"zero_low_word": true`; no other energy point uses it.

Use this exact FC03 energy block alongside the unchanged measurement points:

```json
      "active_energy_coarse": {
        "addr": 4096, "from": "active_energy_total", "scale": 1,
        "divide_by_ct": true, "zero_low_word": true
      },
      "reactive_exp_energy_coarse": {
        "addr": 4106, "from": "reactive_exp_energy_total", "scale": 1,
        "divide_by_ct": true, "zero_low_word": true
      },
      "reactive_imp_energy_coarse": {
        "addr": 4116, "from": "reactive_imp_energy_total", "scale": 1,
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
      "exp_ep": {
        "addr": 4136, "from": "exp_energy_total", "scale": 1,
        "divide_by_ct": true
      },
      "exp_ep_l1": {
        "addr": 4138, "from": "exp_energy_l1", "scale": 1,
        "divide_by_ct": true
      },
      "exp_ep_l2": {
        "addr": 4140, "from": "exp_energy_l2", "scale": 1,
        "divide_by_ct": true
      },
      "exp_ep_l3": {
        "addr": 4142, "from": "exp_energy_l3", "scale": 1,
        "divide_by_ct": true
      },
      "net_exp_ep": {
        "addr": 4144, "from": "net_exp_energy_total", "scale": 1,
        "divide_by_ct": true
      },
      "reactive_imp_energy_coarse_alias": {
        "addr": 4156, "from": "reactive_imp_energy_total", "scale": 1,
        "divide_by_ct": true, "zero_low_word": true
      },
      "reactive_exp_energy_coarse_alias": {
        "addr": 4176, "from": "reactive_exp_energy_total", "scale": 1,
        "divide_by_ct": true, "zero_low_word": true
      }
```

Replace `dtsu_sigen_ext_energy.points` with:

```json
      "active_energy_coarse": {
        "addr": 6144, "from": "active_energy_total", "scale": 1,
        "zero_low_word": true
      },
      "reactive_exp_energy_coarse": {
        "addr": 6154, "from": "reactive_exp_energy_total", "scale": 1,
        "zero_low_word": true
      },
      "reactive_imp_energy_coarse": {
        "addr": 6164, "from": "reactive_imp_energy_total", "scale": 1,
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
      "exp_ep_l1": {"addr": 6186, "from": "exp_energy_l1", "scale": 1},
      "exp_ep_l2": {"addr": 6188, "from": "exp_energy_l2", "scale": 1},
      "exp_ep_l3": {"addr": 6190, "from": "exp_energy_l3", "scale": 1},
      "net_exp_ep": {
        "addr": 6192, "from": "net_exp_energy_total", "scale": 1
      },
      "reactive_imp_energy_coarse_alias": {
        "addr": 6204, "from": "reactive_imp_energy_total", "scale": 1,
        "zero_low_word": true
      },
      "reactive_exp_energy_coarse_alias": {
        "addr": 6224, "from": "reactive_exp_energy_total", "scale": 1,
        "zero_low_word": true
      }
```

Do not add manual zero points. Sequential datastore gaps stay zero.

- [ ] **Step 6: Run all map consumers**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_server.py tests/test_rtudebug.py tests/test_app.py -q -k "not test_run_app_pings_watchdog_during_initial_connect_retry"
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit the physical map**

```powershell
git add -- config/registers.json tests/test_config.py tests/test_server.py tests/test_rtudebug.py
git commit -m "fix: serve physical Sigen export energy fields"
```

---

### Task 3: Complete Static Simulation, Documentation, and Regression

**Files:**
- Modify: `src/nd45_dtsu666/config.py:13-48`
- Modify: `config/config.json:34-39`
- Modify: `tests/test_config.py:162-219`
- Modify: `tests/test_static_debug.py:89-121`
- Modify: `README.md:113-135`
- Modify: `docs/register-map.md:30-55`
- Modify: `docs/register-map.md:106-186`

**Interfaces:**
- Consumes: complete target maps from Task 2.
- Produces: a checked-in simulation that can drive every corrected register.

- [ ] **Step 1: Write static configuration tests**

Update the seed assertions:

```python
    assert cfg.static_debug.values["exp_energy_total"] == 3.0
    assert cfg.static_debug.values["exp_energy_l1"] == 0.8
    assert cfg.static_debug.values["exp_energy_l2"] == 1.0
    assert cfg.static_debug.values["exp_energy_l3"] == 1.0
    assert cfg.static_debug.values["reactive_imp_energy_total"] == 1.2
    assert cfg.static_debug.values["reactive_exp_energy_total"] == 2.8
```

Replace the aggregate reactive acceptance test with:

```python
def test_static_debug_accepts_independent_directional_energy_values():
    configured = StaticDebugConf(values={
        "exp_energy_l1": 0.8,
        "exp_energy_l2": 1.0,
        "exp_energy_l3": 1.0,
        "reactive_imp_energy_total": 1.2,
        "reactive_exp_energy_total": 2.8,
    })
    assert configured.values["exp_energy_l1"] == 0.8
    assert configured.values["reactive_imp_energy_total"] == 1.2
    assert configured.values["reactive_exp_energy_total"] == 2.8
```

The rejection parameter list becomes:

```python
    [
        "active_energy_total",
        "net_imp_energy_total",
        "net_exp_energy_total",
        "reactive_energy_total",
    ]
```

- [ ] **Step 2: Extend the end-to-end static test**

Assert the canonical values and registers:

```python
        assert values["reactive_imp_energy_total"] == 1.2
        assert values["reactive_exp_energy_total"] == 2.8
        assert values["active_energy_total"] == pytest.approx(10.0)
        assert values["net_exp_energy_total"] == pytest.approx(3.0)

        assert registers_to_float(
            slave.getValues(4, 0x1828, 2), "big", "big"
        ) == pytest.approx(3.0)
        assert registers_to_float(
            slave.getValues(4, 0x182A, 2), "big", "big"
        ) == pytest.approx(0.8)
        assert registers_to_float(
            slave.getValues(4, 0x182C, 2), "big", "big"
        ) == pytest.approx(1.0)
        assert registers_to_float(
            slave.getValues(4, 0x182E, 2), "big", "big"
        ) == pytest.approx(1.0)
```

Also assert that `0x1814` and `0x183C` have equal coarse high words, and
`0x180A` and `0x1850` have equal coarse high words.

- [ ] **Step 3: Run static RED tests**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_config.py::test_load_config_reads_seed tests/test_config.py::test_static_debug_accepts_independent_directional_energy_values tests/test_config.py::test_static_debug_rejects_derived_or_obsolete_energy_inputs tests/test_static_debug.py::test_static_pipeline_serves_complete_reverse_flow_energy_image -v
```

Expected: failures because the allowlist and seed still use the obsolete
aggregate and omit phase export.

- [ ] **Step 4: Update the allowlist and seed**

In `STATIC_DEBUG_VALUE_KEYS`, remove:

```python
        "reactive_energy_total",
```

Add:

```python
        "exp_energy_l1",
        "exp_energy_l2",
        "exp_energy_l3",
        "reactive_imp_energy_total",
        "reactive_exp_energy_total",
```

In `config/config.json`, use:

```json
      "imp_energy_total": 7.0,
      "imp_energy_l1": 2.0,
      "imp_energy_l2": 2.4,
      "imp_energy_l3": 2.6,
      "exp_energy_total": 3.0,
      "exp_energy_l1": 0.8,
      "exp_energy_l2": 1.0,
      "exp_energy_l3": 1.0,
      "reactive_imp_energy_total": 1.2,
      "reactive_exp_energy_total": 2.8
```

- [ ] **Step 5: Update operator documentation**

Replace every `reactive_energy_total` production reference with the two exact
directional formulas. Replace all zero claims for `0x1028`-`0x102E`,
`0x182A`-`0x182E`, `0x1014`, `0x103C`, `0x1814`, and `0x183C` with the
address tables from the design. State that `0x1000` is combined active energy,
not import-only energy.

Update the README static-key list to match `STATIC_DEBUG_VALUE_KEYS` exactly.
Document that the checked-in image represents about `7 kWh` import,
`3 kWh` export, `0.8/1.0/1.0 kWh` phase export, `1.2 kvarh` Q+, and
`2.8 kvarh` Q-.

- [ ] **Step 6: Run consistency and JSON checks**

```powershell
rg -n "reactive_energy_total|constant zero|zero-only phase|CT-side import" README.md docs/register-map.md config src tests
.venv\Scripts\python.exe -m json.tool config/config.json > $null
.venv\Scripts\python.exe -m json.tool config/registers.json > $null
git diff --check
```

Expected: no obsolete production statements; only historical design/plan
files under `docs/superpowers/` may contain the old names.

- [ ] **Step 7: Run focused and portable full suites**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_poller.py tests/test_config.py tests/test_server.py tests/test_rtudebug.py tests/test_static_debug.py tests/test_diagnostics.py -q
.venv\Scripts\python.exe -m pytest -q -k "not test_run_app_pings_watchdog_during_initial_connect_retry and not test_notify_ready_sends_datagram and not test_notify_watchdog_sends_datagram and not test_notify_swallows_oserror_from_dead_socket and not test_watchdog_loop_pings_while_heartbeat_fresh and not test_watchdog_loop_withholds_ping_when_heartbeat_stale"
```

Expected: all selected tests pass with no warnings.

- [ ] **Step 8: Commit the simulator and documentation**

```powershell
git add -- config/config.json src/nd45_dtsu666/config.py tests/test_config.py tests/test_static_debug.py README.md docs/register-map.md
git commit -m "feat: simulate complete directional Sigen energy"
```

- [ ] **Step 9: Verify final repository state**

```powershell
git status --short --branch
git log -5 --oneline
```

Expected: `main` contains the design, plan, and three implementation commits;
only the pre-existing untracked `tmp/` directory may remain.
