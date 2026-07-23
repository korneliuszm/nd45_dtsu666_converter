# ND45 Real Apparent Power Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace calculated apparent power with all four float32 apparent-power measurements read directly from the ND45.

**Architecture:** Extend the configuration-driven ND45 source map with registers 60, 84, 108, and 132, which are already covered by the existing 50..145 read. Restrict common derived-value processing to net energy, while static debug applies an omission-only apparent-power fallback before calling it.

**Tech Stack:** Python 3, pytest, pymodbus, JSON register maps, Markdown documentation

## Global Constraints

- ND45 registers `0060`, `0084`, `0108`, and `0132` are float32 values in VA.
- No additional Modbus request may be added.
- ND45 register `0132` is authoritative for `s_total`; do not sum phases in live mode.
- Existing DTSU FC03 and Sigen FC04 target addresses and scales remain unchanged.
- Explicit static-debug `s_*` values must not be overwritten.

---

### Task 1: Prove direct ND45 apparent-power decoding

**Files:**
- Modify: `tests/test_poller.py`

**Interfaces:**
- Consumes: `poll_once(client, source, slave) -> dict[str, float]`
- Produces: regression coverage for canonical `s_l1`, `s_l2`, `s_l3`, and `s_total`

- [ ] **Step 1: Replace the calculated-power test with a direct-reading test**

```python
async def test_poll_once_reads_apparent_power_from_nd45():
    src = load_registers("config/registers.json").nd45_source
    image = _image_for({
        50: 230.0, 52: 2.0,
        60: 471.0,
        84: 582.0,
        108: 693.0,
        132: 1801.0,
    })
    values = await poll_once(FakeClient(image), src, slave=1)
    assert values["s_l1"] == pytest.approx(471.0)
    assert values["s_l2"] == pytest.approx(582.0)
    assert values["s_l3"] == pytest.approx(693.0)
    assert values["s_total"] == pytest.approx(1801.0)
```

- [ ] **Step 2: Run the test and verify RED**

Run: `pytest tests/test_poller.py::test_poll_once_reads_apparent_power_from_nd45 -v`

Expected: FAIL because the current source map lacks the four `s_*` points and
`compute_derived()` replaces them with calculated values.

- [ ] **Step 3: Add the four ND45 source points**

Add to `config/registers.json` under `nd45_source.points`:

```json
"s_l1":    {"addr": 60},
"s_l2":    {"addr": 84},
"s_l3":    {"addr": 108},
"s_total": {"addr": 132}
```

- [ ] **Step 4: Stop deriving apparent power in the live pipeline**

Change `compute_derived()` in `src/nd45_dtsu666/nd45_poller.py` so it only
sets:

```python
values["net_imp_energy_total"] = max(imp - exp, 0.0)
values["net_exp_energy_total"] = max(exp - imp, 0.0)
```

Update its docstring to describe net energy only.

- [ ] **Step 5: Run the focused poller tests and verify GREEN**

Run: `pytest tests/test_poller.py -v`

Expected: all poller tests pass.

### Task 2: Preserve omission-only apparent-power fallback in static debug

**Files:**
- Modify: `src/nd45_dtsu666/static_debug.py`
- Modify: `tests/test_static_debug.py`

**Interfaces:**
- Consumes: `expand_static_values(registers, configured) -> dict[str, float]`
- Produces: explicit-value-preserving debug values with calculated defaults

- [ ] **Step 1: Add tests for omitted and explicit apparent power**

```python
def test_expand_static_values_derives_omitted_apparent_power():
    registers = load_registers("config/registers.json")
    values = expand_static_values(registers, {
        "u_l1": 230.0, "i_l1": 2.0,
        "u_l2": 231.0, "i_l2": 3.0,
        "u_l3": 232.0, "i_l3": 4.0,
    })
    assert values["s_l1"] == pytest.approx(460.0)
    assert values["s_l2"] == pytest.approx(693.0)
    assert values["s_l3"] == pytest.approx(928.0)
    assert values["s_total"] == pytest.approx(2081.0)


def test_expand_static_values_preserves_explicit_apparent_power():
    registers = load_registers("config/registers.json")
    values = expand_static_values(registers, {
        "u_l1": 230.0, "i_l1": 2.0,
        "s_l1": 499.0, "s_l2": 599.0, "s_l3": 699.0,
        "s_total": 1900.0,
    })
    assert values["s_l1"] == pytest.approx(499.0)
    assert values["s_l2"] == pytest.approx(599.0)
    assert values["s_l3"] == pytest.approx(699.0)
    assert values["s_total"] == pytest.approx(1900.0)
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest tests/test_static_debug.py -v`

Expected: the explicit-value test fails because current shared derivation
overwrites configured `s_*` values.

- [ ] **Step 3: Implement omission-only fallback**

In `expand_static_values()`, retain the original configured key set before
zero-filling. For each omitted phase `s_l1/s_l2/s_l3`, assign
`abs(u_phase * i_phase)`. If `s_total` was omitted, assign the sum of the
effective three phase values. Then call `compute_derived(values)` for net
energy.

- [ ] **Step 4: Run static-debug tests and verify GREEN**

Run: `pytest tests/test_static_debug.py -v`

Expected: all static-debug tests pass.

### Task 3: Align documentation and verify the repository

**Files:**
- Modify: `README.md`
- Modify: `docs/register-map.md`

**Interfaces:**
- Consumes: the implemented source register map and behavior
- Produces: accurate operator-facing documentation

- [ ] **Step 1: Replace calculated-power descriptions**

Document `s_l1/s_l2/s_l3/s_total` as direct ND45 measurements from
`60/84/108/132` in VA. State that static debug alone derives omitted apparent
power values as a convenience.

- [ ] **Step 2: Run map and formatting checks**

Run: `python -m json.tool config/registers.json > $null`

Expected: exit code 0.

- [ ] **Step 3: Run the full test suite**

Run: `pytest -v`

Expected: all tests pass with zero failures.

- [ ] **Step 4: Inspect the final diff**

Run: `git diff --check; git diff --stat; git status --short`

Expected: no whitespace errors and only the planned source, tests,
documentation, specification, and plan files are modified.
